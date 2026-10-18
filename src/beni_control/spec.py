"""BENI joint layout, motor parameters and policy interface.

Values are taken from open-beni (python/beni_rl/beni_robot.py,
beni_velocity_flat_env_cfg.py and web/game/physics.mjs) at the pinned commit in
scripts/fetch_open_beni.sh. The policy was trained against exactly these numbers,
so change them only together with a retrained policy.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
OPEN_BENI = ROOT / "third_party" / "open-beni"
BENI_MJCF = OPEN_BENI / "assets" / "beni_base" / "mjcf" / "beni.xml"
POLICY_ONNX = OPEN_BENI / "assets" / "models" / "policy.onnx"

# Policy output order.
ACTION_JOINTS = ("L1", "R1", "L2", "R2", "LW", "RW", "head_yaw_joint")
# Observation order: positions use the first five, velocities all seven.
OBS_JOINTS = ("L1", "R1", "head_yaw_joint", "L2", "R2", "LW", "RW")
WHEELS = ("LW", "RW")

ACTION_SCALE = {"L1": 0.25, "R1": 0.25, "L2": 0.25, "R2": 0.25, "LW": 20.0, "RW": 20.0, "head_yaw_joint": 0.5}
DEFAULT_POS = {"L1": 0.3, "R1": 0.3, "L2": 0.5, "R2": 0.5, "LW": 0.0, "RW": 0.0, "head_yaw_joint": 0.0, "head_roll_joint": 0.0}

PHYSICS_DT = 0.002
DECIMATION = 5
CONTROL_DT = PHYSICS_DT * DECIMATION  # 100 Hz
OBS_DIM = 40
# 0.8 x the joint range, like mjlab's soft_joint_pos_limit_factor.
HEAD_YAW_LIMIT = 0.8 * 5.2
ACTION_DIM = 7


@dataclass(frozen=True)
class Motor:
    stiffness: float
    damping: float
    effort_limit: float
    armature: float
    frictionloss: float
    # Torque limit including the passive knee spring; defaults to effort_limit.
    simulation_effort_limit: float | None = None
    spring_scale: float = 0.0

    @property
    def total_limit(self) -> float:
        return self.effort_limit if self.simulation_effort_limit is None else self.simulation_effort_limit


_HIP = Motor(stiffness=0.5, damping=0.02, effort_limit=1.375, armature=0.0001625, frictionloss=0.02)
_KNEE = Motor(stiffness=0.5, damping=0.02, effort_limit=3.6, armature=0.0005375, frictionloss=0.02,
              simulation_effort_limit=4.6, spring_scale=1.0)
_WHEEL = Motor(stiffness=0.0, damping=0.004, effort_limit=0.6, armature=0.0, frictionloss=0.005)

MOTORS = {
    "L1": _HIP, "R1": _HIP,
    "L2": _KNEE, "R2": _KNEE,
    "LW": _WHEEL, "RW": _WHEEL,
    "head_yaw_joint": Motor(stiffness=3.0, damping=0.05, effort_limit=0.1, armature=0.0, frictionloss=0.0),
    # The real gimbal follows roll mechanically.
    "head_roll_joint": Motor(stiffness=2.0, damping=0.0, effort_limit=1.0, armature=0.0, frictionloss=0.0),
}


@dataclass
class Command:
    """High-level command, clipped to the ranges the policy was trained on."""

    vx: float = 0.0  # forward speed, m/s
    wz: float = 0.0  # yaw rate, rad/s
    # Head yaw target relative to the body, rad. None leaves the head to the
    # policy. The policy never learned to track a head command, so a set value
    # overrides its head output directly.
    head_yaw: float | None = None
    pitch: float = 0.0  # body pose, rad
    roll: float = 0.0
    height: float = 0.11  # m

    def clipped(self) -> "Command":
        return Command(
            vx=float(np.clip(self.vx, -2.0, 2.0)),
            wz=float(np.clip(self.wz, -5.0, 5.0)),
            head_yaw=None if self.head_yaw is None else float(np.clip(self.head_yaw, -HEAD_YAW_LIMIT, HEAD_YAW_LIMIT)),
            pitch=float(np.clip(self.pitch, -0.1, 0.1)),
            roll=float(np.clip(self.roll, -0.1, 0.1)),
            height=float(np.clip(self.height, 0.10, 0.12)),
        )


@dataclass
class JointTargets:
    """What the controller asks the hardware for: position targets for legs and
    head, velocity targets for wheels. All dicts are keyed by joint name."""

    position: dict[str, float]
    velocity: dict[str, float]


@dataclass
class RobotState:
    base_gyro: np.ndarray  # (3,) body-frame angular velocity, rad/s
    base_quat: np.ndarray  # (4,) wxyz orientation of the base IMU
    head_gyro: np.ndarray  # (3,) head IMU angular velocity, rad/s
    joint_pos: dict[str, float]  # encoder angles, rad (zero = encoder zero)
    joint_vel: dict[str, float]  # rad/s


def knee_spring_torque(theta: float, scale: float = 1.0) -> float:
    angle = theta - 1.0
    return scale * (0.22 * np.sin(angle) + 0.15 * np.sin(2.0 * angle))
