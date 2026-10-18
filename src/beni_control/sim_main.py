"""Run the simulated Beni in real time, driven over UDP.

    uv run mjpython -m beni_control.sim_main          # with the MuJoCo viewer (macOS needs mjpython)
    uv run beni-sim --headless                         # no window
"""

import argparse
import time

import numpy as np

from .backend import SimBackend
from .command_server import DEFAULT_PORT, CommandServer
from .controller import Controller, projected_gravity
from .spec import CONTROL_DT, Command, RobotState

TELEMETRY_EVERY = 5  # control steps -> 20 Hz


def telemetry(sim: SimBackend, controller: Controller, command: Command, state: RobotState) -> dict:
    gravity = projected_gravity(state.base_quat)
    return {
        "t": round(float(sim.data.time), 3),
        "command": {"vx": round(controller.vx, 3), "wz": round(controller.wz, 3), "head_yaw": command.head_yaw,
                    "height": command.height},
        "imu": {"gyro": np.round(state.base_gyro, 3).tolist(), "gravity": np.round(gravity, 3).tolist(),
                "tilt_deg": round(float(np.degrees(np.arccos(np.clip(-gravity[2], -1, 1)))), 1)},
        "joints": {"pos": {k: round(v, 3) for k, v in state.joint_pos.items()},
                   "vel": {k: round(v, 2) for k, v in state.joint_vel.items()}},
        # Simulation-only ground truth.
        "sim": {"pos": np.round(sim.base_position, 3).tolist(),
                "vel_body": np.round(sim.base_velocity_body, 3).tolist()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--headless", action="store_true", help="run without the viewer")
    parser.add_argument("--host", default="127.0.0.1", help="UDP bind address (0.0.0.0 to accept other machines)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    sim = SimBackend()
    controller = Controller(sim)
    server = CommandServer(args.host, args.port)
    print(f"Beni simulation listening on udp://{args.host}:{args.port}")

    viewer = None
    if not args.headless:
        import mujoco
        import mujoco.viewer

        viewer = mujoco.viewer.launch_passive(sim.model, sim.data, show_left_ui=False, show_right_ui=False)
        viewer.cam.type = mujoco.mjtCamera.mjCAMERA_TRACKING
        viewer.cam.trackbodyid = mujoco.mj_name2id(sim.model, mujoco.mjtObj.mjOBJ_BODY, "base_link")
        viewer.cam.distance, viewer.cam.elevation, viewer.cam.azimuth = 1.2, -15.0, 135.0

    step = 0
    next_tick = time.perf_counter()
    try:
        while viewer is None or viewer.is_running():
            if server.take_reset():
                controller.reset()
            command = server.current()
            try:
                state = controller.step(command)
            except RuntimeError as error:
                print(f"{error}; resetting")
                controller.reset()
                continue
            if step % TELEMETRY_EVERY == 0:
                server.publish(telemetry(sim, controller, command, state))
                if viewer is not None:
                    viewer.sync()
            step += 1
            next_tick += CONTROL_DT
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.1:
                next_tick = time.perf_counter()  # fell behind; don't try to catch up
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        if viewer is not None:
            viewer.close()


if __name__ == "__main__":
    main()
