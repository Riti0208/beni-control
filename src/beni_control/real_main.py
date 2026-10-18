"""Drive the real Beni from the PC. Drop-in replacement for beni-sim: it accepts
the same UDP/JSON commands (so beni-teleop works unchanged) and forwards them to
the robot as the app's 16-byte control word over UCP.

    uv run beni-real                       # robot at 192.168.7.1 (join its Wi-Fi first)
    uv run beni-real --robot 127.0.0.1     # against beni-mock-robot for a dry run

Safety: the robot starts locked and paused, like the app's own fallback. It only
drives after a client sends {"cmd": "arm"}, and it locks again on {"cmd": "disarm"},
when every client goes quiet for DISARM_AFTER seconds, or when this program exits.

Unverified (docs/robot-protocol.md §8): the sign of each axis (#1), the stick to
m/s scale (#13), and whether the robot accepts a session that skipped activation
(#11). The defaults below follow the docs' best estimate; check them with the
wheels off the ground first.
"""

import argparse
import math
import signal
import time
from dataclasses import dataclass

import numpy as np

from . import ucp
from .command_server import DEFAULT_PORT, CommandServer
from .controller import VX_STEP, WZ_STEP
from .robot_link import HB_PERIOD, ROBOT_ADDR, LinkError, RobotLink
from .spec import CONTROL_DT, HEAD_YAW_LIMIT, Command

DISARM_AFTER = 1.0
TELEMETRY_EVERY = 3  # heartbeats -> ~17 Hz
# The controller's slew limits are per 100 Hz step; scale them to the 50 Hz heartbeat.
SLEW_SCALE = HB_PERIOD / CONTROL_DT


@dataclass
class Scale:
    vx_full: float = 2.0  # m/s at channel0 = 1.0 (max_speed parameter, §5.1)
    wz_full: float = math.radians(170.0)  # rad/s at channel1 = 1.0 (max_yaw_rate, §5.1)
    vx_sign: float = 1.0
    wz_sign: float = 1.0
    height_raw: int = 16  # 4..31 on the wire; the app seeds it from the robot, which we can't read yet
    gear: int = 0  # max_speed field, 0..2; lowest by default


def control_fields(command: Command, armed: bool, scale: Scale) -> dict:
    """Map a Command to the control word fields (§3.3). Unarmed means the app's
    safe state: sticks centred, lock and pause set."""
    if not armed:
        return {"islock": 1, "ispause": 1, "height": scale.height_raw, "max_speed": scale.gear}
    head = 0.0 if command.head_yaw is None else command.head_yaw / HEAD_YAW_LIMIT
    return {
        "channel0": scale.vx_sign * command.vx / scale.vx_full,
        "channel1": scale.wz_sign * command.wz / scale.wz_full,
        # Whether the robot treats channel2 as a head angle or a head rate is unknown.
        "channel2": head,
        "roll": command.roll / 0.1,
        "height": scale.height_raw,
        "max_speed": scale.gear,
    }


class Slew:
    def __init__(self):
        self.vx = self.wz = 0.0

    def step(self, command: Command) -> Command:
        command = command.clipped()
        self.vx += float(np.clip(command.vx - self.vx, -VX_STEP * SLEW_SCALE, VX_STEP * SLEW_SCALE))
        self.wz += float(np.clip(command.wz - self.wz, -WZ_STEP * SLEW_SCALE, WZ_STEP * SLEW_SCALE))
        command.vx, command.wz = self.vx, self.wz
        return command


def connect(link: RobotLink) -> None:
    while True:
        try:
            link.connect(timeout=5.0)
            return
        except LinkError:
            print("no answer from the robot yet; retrying (Ctrl-C to quit)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--robot", default=ROBOT_ADDR[0], help="robot IP (default 192.168.7.1)")
    parser.add_argument("--robot-port", type=int, default=ROBOT_ADDR[1])
    parser.add_argument("--host", default="127.0.0.1", help="UDP bind address for clients")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--vx-full", type=float, default=Scale.vx_full, help="m/s at full forward stick")
    parser.add_argument("--wz-full", type=float, default=Scale.wz_full, help="rad/s at full turn stick")
    parser.add_argument("--invert-vx", action="store_true")
    parser.add_argument("--invert-wz", action="store_true")
    parser.add_argument("--height-raw", type=int, default=Scale.height_raw, choices=range(4, 32), metavar="4..31")
    parser.add_argument("--gear", type=int, default=Scale.gear, choices=range(3), help="speed range 0..2")
    args = parser.parse_args()
    scale = Scale(args.vx_full, args.wz_full, -1.0 if args.invert_vx else 1.0, -1.0 if args.invert_wz else 1.0,
                  args.height_raw, args.gear)

    # Treat SIGTERM like Ctrl-C so the robot is locked on the way out.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    server = CommandServer(args.host, args.port)
    link = RobotLink((args.robot, args.robot_port))
    print(f"connecting to udp://{args.robot}:{args.robot_port} ...")
    connect(link)
    print(f"connected; clients on udp://{args.host}:{args.port}; LOCKED until a client arms")

    slew, armed, beat = Slew(), False, 0
    next_tick = time.perf_counter()
    try:
        while True:
            link.poll()
            if not link.alive:
                print("robot went quiet; reconnecting (locked)")
                armed = False
                connect(link)
            if server.take("disarm") or server.idle_for() > DISARM_AFTER:
                if armed:
                    print("disarmed")
                armed = False
            if server.take("arm") and not armed:
                armed, slew = True, Slew()
                print("ARMED")
            command = slew.step(server.current()) if armed else Command()
            fields = control_fields(command, armed and not link.seeding, scale)
            link.send(ucp.encode_control_word(**fields))
            if beat % TELEMETRY_EVERY == 0:
                server.publish({
                    "t": round(time.monotonic() - link.connected_at, 2),
                    "command": {"vx": round(command.vx, 3), "wz": round(command.wz, 3), "head_yaw": command.head_yaw},
                    "link": {"armed": armed, "seeding": link.seeding, "alive": link.alive,
                             "tx": link.tx_packets, "rx": link.rx_packets},
                })
            beat += 1
            next_tick += HB_PERIOD
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.1:
                next_tick = time.perf_counter()
    except KeyboardInterrupt:
        pass
    finally:
        # Leave the robot locked: a short burst of safe heartbeats before closing.
        for _ in range(10):
            link.send(ucp.encode_control_word(**control_fields(Command(), False, scale)))
            time.sleep(HB_PERIOD)
        link.close()
        server.close()


if __name__ == "__main__":
    main()
