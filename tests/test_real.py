"""The real-robot path against a fake robot on localhost: handshake, seeding,
the locked default, and the Command -> control word mapping."""

import math
import socket
import time

import pytest

from beni_control import ucp
from beni_control.real_main import Scale, control_fields
from beni_control.robot_link import SEED_PAYLOAD, SEED_TIME, RobotLink
from beni_control.spec import Command


@pytest.fixture
def robot():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.05)
    yield sock
    sock.close()


def answer_sync(robot):
    data, addr = robot.recvfrom(2048)
    assert data == ucp.SYNC
    robot.sendto(ucp.ACK, addr)
    return addr


def connect(robot):
    link = RobotLink(robot.getsockname())
    link.sock.sendto(ucp.SYNC, link.addr)  # connect() resends; prime the first one
    addr = answer_sync(robot)
    link.connect(timeout=1.0)
    return link, addr


def received(robot):
    out = []
    while True:
        try:
            out.append(robot.recvfrom(2048)[0])
        except TimeoutError:
            return out


def test_seeds_then_sends_control_word(robot):
    link, _ = connect(robot)
    word = ucp.encode_control_word(**control_fields(Command(), False, Scale()))
    link.send(word)
    packets = [p for p in received(robot) if p != ucp.SYNC]
    header = ucp.parse_header(packets[-1])
    assert header.is_hb and header.length == len(packets[-1]) == 24
    assert packets[-1][12:13] == SEED_PAYLOAD

    time.sleep(SEED_TIME)
    link.send(word)
    packet = received(robot)[-1]
    assert len(packet) == ucp.HB_LEN
    assert ucp.parse_ack_block(packet) is not None
    fields = ucp.decode_control_word(packet[12:28])
    assert fields["islock"] == fields["ispause"] == 1
    assert fields["channel0"] == fields["channel1"] == 1000
    link.close()


def test_acks_robot_packets(robot):
    link, addr = connect(robot)
    for sn in (7, 8):
        robot.sendto(ucp.pack_header(0, 0, sn, ucp.HEADER_LEN + 1) + b"x", addr)
    time.sleep(0.05)
    assert len(link.poll()) == 2
    assert link.rcv_next == 9 and link.alive
    link.send(bytes(16))
    assert ucp.parse_header(received(robot)[-1]).una == 9
    link.close()


def test_unarmed_is_locked_and_centred():
    fields = control_fields(Command(vx=1.0, wz=1.0), False, Scale())
    raw = ucp.decode_control_word(ucp.encode_control_word(**fields))
    assert raw["islock"] == raw["ispause"] == 1
    assert raw["channel0"] == raw["channel1"] == raw["channel2"] == 1000


def test_armed_maps_speed_to_sticks():
    scale = Scale(vx_full=2.0, wz_full=math.radians(170), wz_sign=-1.0)
    fields = control_fields(Command(vx=1.0, wz=math.radians(85)), True, scale)
    raw = ucp.decode_control_word(ucp.encode_control_word(**fields))
    assert raw["islock"] == raw["ispause"] == 0
    assert raw["channel0"] == 1500  # half of full forward
    assert raw["channel1"] == 500  # half turn, inverted
    assert raw["height"] == scale.height_raw and raw["max_speed"] == 0
