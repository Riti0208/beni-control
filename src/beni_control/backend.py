"""Robot backends. The controller only talks to this interface, so the MuJoCo
simulation can later be swapped for a real Beni without touching anything else."""

from collections import deque
from typing import Protocol
import xml.etree.ElementTree as ET

import mujoco
import numpy as np

from .spec import BENI_MJCF, DECIMATION, DEFAULT_POS, MOTORS, PHYSICS_DT, WHEELS, JointTargets, RobotState, knee_spring_torque


class RobotBackend(Protocol):
    def reset(self) -> None: ...

    def read_state(self) -> RobotState: ...

    def apply(self, targets: JointTargets) -> None:
        """Send joint targets and return once the next control step is due."""
        ...


def build_sim_xml() -> str:
    """Turn open-beni's beni.xml into a standalone scene with one torque motor
    per joint, mirroring web/scripts/export-assets.py, plus a floor and lights."""
    root = ET.parse(BENI_MJCF).getroot()
    asset_dir = BENI_MJCF.parent.parent
    root.find("compiler").attrib.update(
        meshdir=str(asset_dir / "meshes"), texturedir=str(asset_dir / "textures"), autolimits="true")
    # Same solver settings as the mjlab training environment.
    ET.SubElement(root, "option", timestep=str(PHYSICS_DT), gravity="0 0 -9.81", integrator="implicitfast",
                  iterations="10", ls_iterations="20", ccd_iterations="50", cone="pyramidal")
    old = root.find("actuator")
    if old is not None:
        root.remove(old)
    actuators = ET.SubElement(root, "actuator")
    for joint in root.findall(".//joint"):
        name = joint.get("name")
        if name not in MOTORS:
            continue
        motor = MOTORS[name]
        joint.attrib.update(ref="0", actuatorfrclimited="false", armature=str(motor.armature),
                            frictionloss=str(motor.frictionloss), damping="0")
        joint.attrib.pop("actuatorfrcrange", None)
        ET.SubElement(actuators, "motor", name=name, joint=name, gear="1", ctrllimited="true",
                      ctrlrange=f"{-motor.total_limit} {motor.total_limit}")
    for name in WHEELS:
        for geom in root.find(f'.//body[@name="{name}_Link"]').findall("geom"):
            if geom.get("contype", "1") != "0" or geom.get("conaffinity", "1") != "0":
                # Let wheel friction override the ground's.
                geom.attrib.update(name=f"{name}_collision", priority="1")

    asset = root.find("asset")
    ET.SubElement(asset, "texture", name="grid", type="2d", builtin="checker", rgb1=".25 .27 .3", rgb2=".32 .34 .38",
                  width="512", height="512", mark="edge", markrgb=".45 .47 .5")
    ET.SubElement(asset, "material", name="grid", texture="grid", texrepeat="20 20", reflectance="0.1")
    ET.SubElement(asset, "texture", name="sky", type="skybox", builtin="gradient", rgb1=".55 .65 .8", rgb2=".1 .12 .15",
                  width="512", height="512")
    world = root.find("worldbody")
    world.insert(0, ET.Element("geom", name="floor", type="plane", size="0 0 0.05", material="grid"))
    world.insert(0, ET.Element("light", pos="1 -1 3", dir="-0.3 0.3 -1", diffuse="0.9 0.9 0.9", directional="true"))
    return ET.tostring(root, encoding="unicode")


class SimBackend:
    """MuJoCo BENI with the PD motors, knee spring and command delay the policy
    was trained with (ported from open-beni web/game/physics.mjs)."""

    # 2 substeps = 4 ms, inside mjlab's 4-10 ms command-delay range.
    COMMAND_DELAY_STEPS = 2
    # Browser calibration that softens landings.
    KNEE_DAMPING_SCALE = 1.2

    def __init__(self):
        self.model = mujoco.MjModel.from_xml_string(build_sim_xml())
        self.data = mujoco.MjData(self.model)
        self.joints = {}
        for name in MOTORS:
            jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)
            self.joints[name] = (
                self.model.jnt_qposadr[jid],
                self.model.jnt_dofadr[jid],
                mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_ACTUATOR, name),
            )
        self.sensors = {
            name: self.model.sensor_adr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_SENSOR, name)]
            for name in ("imu_gyro", "imu_quat", "head_imu_gyro")
        }
        self.reset()

    def reset(self, x: float = 0.0, y: float = 0.0, yaw: float = 0.0) -> None:
        mujoco.mj_resetData(self.model, self.data)
        self.data.qpos[0:3] = (x, y, 0.14)
        self.data.qpos[3:7] = (np.cos(yaw / 2), 0, 0, np.sin(yaw / 2))
        for name, (qadr, _, _) in self.joints.items():
            self.data.qpos[qadr] = DEFAULT_POS[name]
        hold = JointTargets(position={n: DEFAULT_POS[n] for n in MOTORS if n not in WHEELS},
                            velocity={n: 0.0 for n in WHEELS})
        self.pending = deque([hold] * self.COMMAND_DELAY_STEPS)
        mujoco.mj_forward(self.model, self.data)

    def read_state(self) -> RobotState:
        s = self.data.sensordata
        g, q, h = self.sensors["imu_gyro"], self.sensors["imu_quat"], self.sensors["head_imu_gyro"]
        return RobotState(
            base_gyro=s[g:g + 3].copy(),
            base_quat=s[q:q + 4].copy(),
            head_gyro=s[h:h + 3].copy(),
            joint_pos={n: float(self.data.qpos[a]) for n, (a, _, _) in self.joints.items()},
            joint_vel={n: float(self.data.qvel[a]) for n, (_, a, _) in self.joints.items()},
        )

    def _torque(self, name: str, targets: JointTargets) -> float:
        motor = MOTORS[name]
        qadr, vadr, _ = self.joints[name]
        pos, vel = self.data.qpos[qadr], self.data.qvel[vadr]
        damping = motor.damping * (self.KNEE_DAMPING_SCALE if name in ("L2", "R2") else 1.0)
        raw = (motor.stiffness * (targets.position.get(name, DEFAULT_POS[name]) - pos)
               + damping * (targets.velocity.get(name, 0.0) - vel))
        torque = np.clip(raw, -motor.effort_limit, motor.effort_limit)
        if motor.spring_scale:
            torque += knee_spring_torque(pos, motor.spring_scale)
        return float(np.clip(torque, -motor.total_limit, motor.total_limit))

    def apply(self, targets: JointTargets) -> None:
        for _ in range(DECIMATION):
            self.pending.append(targets)
            delayed = self.pending.popleft()
            for name, (_, _, act) in self.joints.items():
                self.data.ctrl[act] = self._torque(name, delayed)
            mujoco.mj_step(self.model, self.data)
        if not np.all(np.isfinite(self.data.qpos)):
            raise RuntimeError("Simulation diverged")
        # mj_step leaves sensors one substep behind qpos/qvel; refresh before the next observation.
        mujoco.mj_forward(self.model, self.data)

    # Ground truth for tests and telemetry; a real robot will not have these.
    @property
    def base_position(self) -> np.ndarray:
        return self.data.qpos[0:3].copy()

    @property
    def base_velocity_body(self) -> np.ndarray:
        w, x, y, z = self.data.qpos[3:7]
        rot = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ])
        return rot.T @ self.data.qvel[0:3]
