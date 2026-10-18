"""UDP/JSON command interface so any program on the PC can drive the robot.

Client -> robot (any subset of fields; omitted fields keep their last value):
    {"vx": 0.8, "wz": 0.5, "head_yaw": 0.3, "height": 0.11, "pitch": 0.0, "roll": 0.0}
    {"head_yaw": null}   give the head back to the policy
    {"cmd": "reset"}     reset the robot (simulation only)
    {"cmd": "arm"}       allow the real robot to drive (it starts locked)
    {"cmd": "disarm"}    lock the real robot again

Robot -> client: telemetry JSON (see sim_main.telemetry) sent to every address
that sent a packet within the last CLIENT_TIMEOUT seconds.

If no packet arrives for WATCHDOG seconds, vx and wz are reset to zero so a
crashed client cannot leave the robot driving. Clients that drive the robot
should therefore resend their command continuously (e.g. at 20 Hz).
"""

import json
import socket
import threading
import time
from dataclasses import replace

from .spec import Command

DEFAULT_PORT = 9870
WATCHDOG = 0.5
CLIENT_TIMEOUT = 2.0
_FIELDS = ("vx", "wz", "head_yaw", "height", "pitch", "roll")


class CommandServer:
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.settimeout(0.2)
        self.lock = threading.Lock()
        self.command = Command()
        self.last_packet = 0.0
        self.clients: dict[tuple, float] = {}
        self.pending: set[str] = set()
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while self.running:
            try:
                payload, addr = self.sock.recvfrom(65536)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            try:
                message = json.loads(payload)
                if not isinstance(message, dict):
                    raise ValueError("expected a JSON object")
                fields = {k: (None if message[k] is None and k == "head_yaw" else float(message[k]))
                          for k in _FIELDS if k in message}
            except (ValueError, TypeError) as error:
                print(f"[command] ignored packet from {addr}: {error}")
                continue
            now = time.monotonic()
            with self.lock:
                self.command = replace(self.command, **fields)
                self.last_packet = now
                self.clients[addr] = now
                if isinstance(message.get("cmd"), str):
                    self.pending.add(message["cmd"])

    def current(self) -> Command:
        with self.lock:
            if time.monotonic() - self.last_packet > WATCHDOG:
                # Clear the stored motion too, so a later partial packet (e.g. only
                # head_yaw) cannot resume the old drive command.
                self.command.vx = self.command.wz = 0.0
            return replace(self.command)

    def take(self, cmd: str) -> bool:
        """Return True once for each received {"cmd": cmd}."""
        with self.lock:
            requested = cmd in self.pending
            self.pending.discard(cmd)
            return requested

    def idle_for(self) -> float:
        """Seconds since the last packet from any client."""
        with self.lock:
            return time.monotonic() - self.last_packet

    def publish(self, telemetry: dict) -> None:
        now = time.monotonic()
        with self.lock:
            self.clients = {a: t for a, t in self.clients.items() if now - t < CLIENT_TIMEOUT}
            clients = list(self.clients)
        payload = json.dumps(telemetry).encode()
        for addr in clients:
            try:
                self.sock.sendto(payload, addr)
            except OSError:
                pass

    def close(self) -> None:
        self.running = False
        self.sock.close()
