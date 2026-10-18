"""UCP link to the real robot: handshake, seeding, and the 50 Hz heartbeat that
carries the control word. See docs/robot-protocol.md §2-§3 for the format.

The caller drives the timing: call `send()` every HB_PERIOD and `poll()` as often
as convenient. Nothing here blocks except `connect()`.
"""

import socket
import time

from . import ucp

ROBOT_ADDR = ("192.168.7.1", 9007)
HB_PERIOD = 0.02  # the app's control_loop runs at 50 Hz
SEED_TIME = 0.5  # the app seeds for up to 500 ms before falling back to lock+pause
SEED_PAYLOAD = b"\xfe"  # LzConstants.kSeedingHeartbeatPayload
LINK_TIMEOUT = 3.0  # no packet from the robot for this long -> handshake again


class LinkError(RuntimeError):
    pass


class RobotLink:
    def __init__(self, addr: tuple[str, int] = ROBOT_ADDR):
        self.addr = (socket.gethostbyname(addr[0]), addr[1])
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sn = 0
        self.rcv_next: int | None = None  # next sn expected from the robot (our una)
        self.connected_at = 0.0
        self.last_rx = 0.0
        self.rx_packets = 0
        self.tx_packets = 0

    def connect(self, timeout: float = 5.0, retry: float = 0.5) -> None:
        """Repeat `ucp:sync` until the robot answers `ucp:ack`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.sendto(ucp.SYNC, self.addr)
            until = min(deadline, time.monotonic() + retry)
            while time.monotonic() < until:
                for data in self._recv_all():
                    if data[:4] == b"ucp:" and data[3:7] == b":ack":
                        self.sn, self.rcv_next = 0, None
                        self.connected_at = self.last_rx = time.monotonic()
                        return
                time.sleep(0.01)
        raise LinkError(f"no ucp:ack from {self.addr[0]}:{self.addr[1]}")

    @property
    def seeding(self) -> bool:
        return time.monotonic() - self.connected_at < SEED_TIME

    @property
    def alive(self) -> bool:
        return time.monotonic() - self.last_rx < LINK_TIMEOUT

    def send(self, control: bytes) -> None:
        """Send one heartbeat. During the seeding window the seed payload goes
        out instead of `control`, as the app does."""
        payload = SEED_PAYLOAD if self.seeding else control
        una = self.rcv_next or 0
        block = ucp.pack_ack_block(una)
        length = ucp.HEADER_LEN + len(payload) + len(block)
        packet = ucp.pack_header(ucp.FLAG_HB, una, self.sn, length) + payload + block
        self.sock.sendto(packet, self.addr)
        self.sn = (self.sn + 1) & 0xFFFF
        self.tx_packets += 1

    def poll(self) -> list[ucp.Packet]:
        """Drain received datagrams, update the ACK state, and return the packets."""
        packets = []
        for data in self._recv_all():
            if data.startswith(b"ucp:"):
                continue  # late handshake reply
            parsed, _rest = ucp.split_packets(data)
            for packet in parsed:
                self._note_received(packet.header.sn)
            packets += parsed
        return packets

    def _note_received(self, sn: int) -> None:
        # Cumulative ACK only; the SACK bitmap is still unverified (§8 #8), so we send 0.
        self.last_rx = time.monotonic()
        self.rx_packets += 1
        if self.rcv_next is None or sn == self.rcv_next:
            self.rcv_next = (sn + 1) & 0xFFFF

    def _recv_all(self) -> list[bytes]:
        out = []
        while True:
            try:
                data, addr = self.sock.recvfrom(65536)
            except (BlockingIOError, InterruptedError):
                return out
            except ConnectionRefusedError:
                continue  # ICMP port unreachable from an earlier send
            if addr[0] == self.addr[0]:
                out.append(data)

    def close(self) -> None:
        self.sock.close()
