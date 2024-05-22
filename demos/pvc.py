# # Premature Ventricular Complexes (PVCs)
import matplotlib.pyplot as plt
import ap_features as apf
from pathlib import Path
import numpy as np
import beat
import dolfin
import pint
import gotranx
from beat.single_cell import get_steady_state

ureg = pint.UnitRegistry()

here = Path(__file__).parent


def delete_old_file(file: str):
    Path(file).unlink(missing_ok=True)
    Path(file).with_suffix(".h5").unlink(missing_ok=True)


model_path = Path("tentusscher_panfilov_2006_epi_cell.py")
if not model_path.is_file():
    here = Path(__file__).parent
    ode = gotranx.load_ode(
        here
        / ".."
        / "odes"
        / "tentusscher_panfilov_2006"
        / "tentusscher_panfilov_2006_epi_cell.ode"
    )
    code = gotranx.cli.gotran2py.get_code(
        ode, scheme=[gotranx.schemes.Scheme.forward_generalized_rush_larsen]
    )
    model_path.write_text(code)

import tentusscher_panfilov_2006_epi_cell

model = tentusscher_panfilov_2006_epi_cell.__dict__


def run(mesh, L, outdir, dx, model, traveling_wave=False):

    D = 0.0005 * ureg("cm**2 / ms")
    Cm = 1.0 * ureg("uF/cm**2")

    parameters = model["init_parameter_values"](stim_start=100.0, stim_period=5000.0)

    dt = 0.01
    nbeats = 50
    fun = model["forward_generalized_rush_larsen"]
    y = get_steady_state(
        fun=fun,
        init_states=model["init_state_values"](),
        parameters=parameters,
        outdir=outdir / "prebeats",
        BCL=1000,
        nbeats=nbeats,
        track_indices=[model["state_index"]("V"), model["state_index"]("Ca_i")],
        dt=dt,
    )

    time = dolfin.Constant(0.0)
    if traveling_wave:
        parameters = model["init_parameter_values"](
            stim_start=100.0, stim_period=5000.0, stim_amplitude=0.0
        )

        I_s_expr = dolfin.Expression(
            "time >= start ? (time <= (duration + start) ? amplitude : 0.0) : 0.0",
            time=time,
            start=100.0,
            duration=2.0,
            amplitude=1.0,
            degree=0,
        )
        subdomain_data = dolfin.MeshFunction("size_t", mesh, mesh.topology().dim() - 1)
        subdomain_data.set_all(0)
        marker = 1
        dolfin.CompiledSubDomain("x[0] < 2 * dx", dx=dx).mark(subdomain_data, 1)

        ds = dolfin.Measure("ds", domain=mesh, subdomain_data=subdomain_data)(marker)
        I_s = beat.base_model.Stimulus(dz=ds, expr=I_s_expr)
    else:
        I_s = dolfin.Constant(0.0)

    V_ode = dolfin.FunctionSpace(mesh, "Lagrange", 1)
    parameters_ode = np.zeros((len(parameters), V_ode.dim()))
    parameters_ode.T[:] = parameters

    g_Kr_index = model["parameter_index"]("g_Kr")
    g_Kr_value = parameters[g_Kr_index]
    parameters_ode[g_Kr_index, :] = (
        dolfin.interpolate(
            dolfin.Expression(
                "g_Kr ? x[0] > L / 2 : 0.0", g_Kr=g_Kr_value, L=L, degree=0
            ),
            V_ode,
        )
        .vector()
        .get_local()
    )

    g_Ks_index = model["parameter_index"]("g_Ks")
    g_Ks_value = parameters[g_Ks_index]
    parameters_ode[g_Ks_index, :] = (
        dolfin.interpolate(
            dolfin.Expression(
                "g_Ks ? x[0] > L / 2 : 0.0", g_Ks=g_Ks_value, L=L, degree=0
            ),
            V_ode,
        )
        .vector()
        .get_local()
    )

    pde = beat.MonodomainModel(
        time=time, mesh=mesh, M=D.magnitude, I_s=I_s, C_m=Cm.magnitude
    )
    ode = beat.odesolver.DolfinODESolver(
        v_ode=dolfin.Function(V_ode),
        v_pde=pde.state,
        fun=fun,
        init_states=y,
        parameters=parameters_ode,
        num_states=len(y),
        v_index=model["state_index"]("V"),
    )
    solver = beat.MonodomainSplittingSolver(pde=pde, ode=ode)

    fname = (outdir / "V.xdmf").as_posix()
    delete_old_file(fname)

    def save(t):
        v = solver.pde.state.vector().get_local()
        print(f"Solve for {t=:.2f}, {v.max() =}, {v.min() = }")
        with dolfin.XDMFFile(mesh.mpi_comm(), fname) as xdmf:
            xdmf.parameters["functions_share_mesh"] = True
            xdmf.parameters["rewrite_function_mesh"] = False
            xdmf.write_checkpoint(
                solver.pde.state,
                "V",
                float(t),
                dolfin.XDMFFile.Encoding.HDF5,
                True,
            )

    t = 0.0
    save_freq = int(1.0 / dt)
    end_time = 5000.0
    i = 0
    while t < end_time + 1e-12:
        # Make sure to save at the same time steps that is used by Ambit

        if i % save_freq == 0:
            save(t)

        solver.step((t, t + dt))
        i += 1
        t += dt


def load_timesteps_from_xdmf(xdmffile):
    import xml.etree.ElementTree as ET

    times = {}
    i = 0
    tree = ET.parse(xdmffile)
    for elem in tree.iter():
        if elem.tag == "Time":
            times[i] = float(elem.get("Value"))
            i += 1

    return times


def post_process(mesh, dx, outdir):
    V = dolfin.Function(dolfin.FunctionSpace(mesh, "CG", 1))
    xdmffile = (outdir / "V.xdmf").as_posix()

    cellnr = [0, 25, 50, 75, 100, 125, 150, 175, 200]
    times = load_timesteps_from_xdmf(xdmffile)
    t = np.array(list(times.values()))

    p1 = 25 * dx
    p2 = 175 * dx
    dp = 150 * dx * ureg("cm")
    tp1 = np.inf
    tp2 = np.inf

    traces = np.zeros((len(times), len(cellnr)))
    with dolfin.XDMFFile(mesh.mpi_comm(), xdmffile) as xdmf:
        for i, ti in enumerate(times):
            xdmf.read_checkpoint(V, "V", ti)
            for j, cell in enumerate(cellnr):
                traces[i, j] = V(cell * dx)

            if V(p1) > 0.0 and tp1 == np.inf:
                tp1 = ti * ureg("ms")
            if V(p2) > 0.0 and tp2 == np.inf:
                tp2 = ti * ureg("ms")

    if not np.isclose(tp1, tp2):
        cv = dp / (tp2 - tp1)
        print(
            f"Conduction velocity:: {cv.to('cm/ms').magnitude} cm/ms "
            f"= {cv.to('m/s').magnitude} m/s"
        )

    # Plot 3D plot for all traces
    fig, ax = plt.subplots()
    for i, cell_index in enumerate(cellnr):
        ax.plot(t, -cell_index / 3 * np.ones_like(t) + traces[:, i])
    ax.set_xlabel("Time (ms)")

    ax.set_ylim(-200, 50)
    fig.tight_layout()
    fig.savefig(outdir / "V_3d.png")


outdir = here / "results-pvc"
mesh_unit = "cm"
dx = 0.015
num_cells = 200
L = num_cells * dx
mesh = dolfin.IntervalMesh(num_cells, 0, L)
run(mesh, L, outdir, dx, model, traveling_wave=True)
post_process(mesh, dx, outdir)
