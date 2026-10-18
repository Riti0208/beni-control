"""Hardware-independent control loop: state + command -> policy -> joint targets."""

import numpy as np

from .backend import RobotBackend
from .policy import Policy
from .spec import ACTION_DIM, ACTION_JOINTS, ACTION_SCALE, DEFAULT_POS, OBS_DIM, OBS_JOINTS, WHEELS, Command, JointTargets, RobotState

# Per-step command slew limits used by the open-beni web client. They keep
# human input inside the envelope the small balancing robot was trained on.
VX_STEP = 0.03
WZ_STEP = 0.06


def projected_gravity(quat_wxyz: np.ndarray) -> np.ndarray:
    w, x, y, z = quat_wxyz
    return np.array([2 * (w * y - x * z), -2 * (y * z + w * x), -(1 - 2 * (x * x + y * y))])


def build_observation(state: RobotState, command: Command, last_action: np.ndarray) -> np.ndarray:
    obs = np.zeros(OBS_DIM, np.float32)
    obs[0:3] = state.base_gyro
    obs[3:6] = projected_gravity(state.base_quat)
    obs[6:9] = (command.vx, 0.0, command.wz)
    for i, name in enumerate(OBS_JOINTS):
        if i < 5:
            obs[9 + i] = state.joint_pos[name] - DEFAULT_POS[name]
        obs[14 + i] = state.joint_vel[name]
    obs[21:28] = last_action
    obs[28:31] = (command.pitch, command.roll, command.height)
    # Gimbal command (yaw, enable). No training reward depends on it, so keep the
    # training default.
    obs[31:33] = (0.0, 1.0)
    obs[33:36] = state.head_gyro
    # 36:40 is the jump command; zero means "no jump requested".
    return obs


def action_to_targets(action: np.ndarray) -> JointTargets:
    position, velocity = {}, {}
    for name, value in zip(ACTION_JOINTS, action):
        target = float(value) * ACTION_SCALE[name]
        if name in WHEELS:
            velocity[name] = target
        else:
            position[name] = DEFAULT_POS[name] + target
    return JointTargets(position=position, velocity=velocity)


class Controller:
    def __init__(self, backend: RobotBackend, policy: Policy | None = None):
        self.backend = backend
        self.policy = policy or Policy()
        self.reset()

    def reset(self) -> None:
        self.backend.reset()
        self.policy.reset()
        self.last_action = np.zeros(ACTION_DIM, np.float32)
        self.vx = 0.0
        self.wz = 0.0

    def step(self, command: Command) -> RobotState:
        """Run one 100 Hz control step and return the state the policy saw."""
        command = command.clipped()
        self.vx += float(np.clip(command.vx - self.vx, -VX_STEP, VX_STEP))
        self.wz += float(np.clip(command.wz - self.wz, -WZ_STEP, WZ_STEP))
        command.vx, command.wz = self.vx, self.wz

        state = self.backend.read_state()
        action = self.policy(build_observation(state, command, self.last_action))
        if command.head_yaw is not None:
            head = ACTION_JOINTS.index("head_yaw_joint")
            action[head] = (command.head_yaw - DEFAULT_POS["head_yaw_joint"]) / ACTION_SCALE["head_yaw_joint"]
        self.backend.apply(action_to_targets(action))
        self.last_action = action
        return state
