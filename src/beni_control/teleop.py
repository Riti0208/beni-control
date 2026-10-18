"""Keyboard teleop over UDP. Each key press nudges the command; it is resent at
20 Hz so the robot's watchdog stays satisfied while this window is open.

    w / s   forward / backward speed  (+-0.25 m/s)
    a / d   turn left / right         (+-0.5 rad/s)
    space   stop (speed and turn to zero)
    q / e   head left / right         (+-0.3 rad)
    c       give the head back to the policy
    z / x   body lower / higher       (+-5 mm)
    r       reset the robot (simulation)
    Esc     quit
"""

import argparse
import curses
import json
import socket
import time

from .command_server import DEFAULT_PORT
from .spec import HEAD_YAW_LIMIT

SEND_PERIOD = 0.05


def clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def run(screen, host: str, port: int) -> None:
    curses.curs_set(0)
    screen.nodelay(True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    target = (host, port)
    command = {"vx": 0.0, "wz": 0.0, "head_yaw": None, "height": 0.11}
    status, last_send = {}, 0.0

    while True:
        reset = False
        key = screen.getch()
        if key == 27:
            break
        match chr(key) if 0 <= key < 256 else "":
            case "w": command["vx"] = clamp(command["vx"] + 0.25, 2.0)
            case "s": command["vx"] = clamp(command["vx"] - 0.25, 2.0)
            case "a": command["wz"] = clamp(command["wz"] + 0.5, 5.0)
            case "d": command["wz"] = clamp(command["wz"] - 0.5, 5.0)
            case " ": command["vx"] = command["wz"] = 0.0
            case "q": command["head_yaw"] = clamp((command["head_yaw"] or 0.0) + 0.3, HEAD_YAW_LIMIT)
            case "e": command["head_yaw"] = clamp((command["head_yaw"] or 0.0) - 0.3, HEAD_YAW_LIMIT)
            case "c": command["head_yaw"] = None
            case "z": command["height"] = max(0.10, command["height"] - 0.005)
            case "x": command["height"] = min(0.12, command["height"] + 0.005)
            case "r":
                reset = True
                command.update(vx=0.0, wz=0.0, head_yaw=None, height=0.11)

        now = time.monotonic()
        if reset or now - last_send >= SEND_PERIOD:
            sock.sendto(json.dumps({**command, **({"cmd": "reset"} if reset else {})}).encode(), target)
            last_send = now
        try:
            while True:
                status = json.loads(sock.recv(65536))
        except (BlockingIOError, ConnectionRefusedError):
            pass

        screen.erase()
        screen.addstr(0, 0, f"Beni teleop -> udp://{host}:{port}")
        head = "policy" if command["head_yaw"] is None else f"{command['head_yaw']:+.2f} rad"
        screen.addstr(2, 0, f"target  vx {command['vx']:+.2f} m/s   wz {command['wz']:+.2f} rad/s   "
                            f"head {head}   height {command['height'] * 1000:.0f} mm")
        if status:
            sim, imu = status.get("sim", {}), status["imu"]
            vel = sim.get("vel_body", [0, 0, 0])
            screen.addstr(3, 0, f"robot   vx {vel[0]:+.2f} m/s   wz {imu['gyro'][2]:+.2f} rad/s   "
                                f"tilt {imu['tilt_deg']:4.1f} deg   t {status['t']:.1f} s")
        else:
            screen.addstr(3, 0, "robot   (no telemetry yet - is beni-sim running?)")
        for i, line in enumerate(__doc__.strip().splitlines()[3:]):
            screen.addstr(5 + i, 0, line)
        screen.refresh()
        time.sleep(0.01)


def main() -> None:
    parser = argparse.ArgumentParser(description="Keyboard teleop for Beni over UDP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    curses.wrapper(run, args.host, args.port)


if __name__ == "__main__":
    main()
