"""End-to-end checks that the ported observation/actuator code reproduces the
behaviour the open-beni policy was trained for."""

import json
import socket
import time

import numpy as np
import pytest

from beni_control.backend import SimBackend
from beni_control.command_server import WATCHDOG, CommandServer
from beni_control.controller import Controller, projected_gravity
from beni_control.spec import Command


@pytest.fixture(scope="module")
def controller():
    return Controller(SimBackend())


def run(controller, command, seconds):
    for _ in range(int(seconds * 100)):
        state = controller.step(command)
    return state


def tilt_deg(state):
    return np.degrees(np.arccos(np.clip(-projected_gravity(state.base_quat)[2], -1, 1)))


def test_stands_still(controller):
    controller.reset()
    state = run(controller, Command(), 3)
    sim = controller.backend
    assert tilt_deg(state) < 5
    assert 0.10 < sim.base_position[2] < 0.12
    assert abs(sim.base_velocity_body[0]) < 0.2


@pytest.mark.parametrize("vx", [1.0, -0.5])
def test_tracks_forward_speed(controller, vx):
    controller.reset()
    state = run(controller, Command(vx=vx), 3)
    assert controller.backend.base_velocity_body[0] == pytest.approx(vx, abs=0.15)
    assert tilt_deg(state) < 10


def test_tracks_yaw_rate(controller):
    controller.reset()
    state = run(controller, Command(wz=2.0), 3)
    assert state.base_gyro[2] == pytest.approx(2.0, abs=0.3)


def test_head_override_keeps_balance(controller):
    controller.reset()
    state = run(controller, Command(vx=0.8, head_yaw=-2.0), 3)
    assert state.joint_pos["head_yaw_joint"] == pytest.approx(-2.0, abs=0.05)
    assert tilt_deg(state) < 10


def test_watchdog_clears_motion():
    server = CommandServer(port=0)
    port = server.sock.getsockname()[1]
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client.sendto(json.dumps({"vx": 1.0, "wz": 1.0}).encode(), ("127.0.0.1", port))
        time.sleep(0.1)
        assert server.current().vx == 1.0
        time.sleep(WATCHDOG + 0.1)
        assert server.current().vx == 0.0
        # A partial packet after the timeout must not resume driving.
        client.sendto(json.dumps({"head_yaw": 0.5}).encode(), ("127.0.0.1", port))
        time.sleep(0.1)
        command = server.current()
        assert (command.vx, command.wz, command.head_yaw) == (0.0, 0.0, 0.5)
    finally:
        client.close()
        server.close()
