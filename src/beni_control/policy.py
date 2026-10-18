"""Recurrent (GRU-512) locomotion policy exported by open-beni."""

from pathlib import Path

import numpy as np
import onnxruntime as ort

from .spec import ACTION_DIM, OBS_DIM, POLICY_ONNX


class Policy:
    def __init__(self, path: Path = POLICY_ONNX):
        self.session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        inputs = self.session.get_inputs()
        if inputs[0].shape != [1, OBS_DIM]:
            raise ValueError(f"Expected obs [1, {OBS_DIM}], got {inputs[0].shape}")
        self.obs_name = inputs[0].name
        self.hidden_name = inputs[1].name if len(inputs) > 1 else None
        self.hidden_shape = inputs[1].shape if self.hidden_name else None
        self.reset()

    def reset(self) -> None:
        self.hidden = np.zeros(self.hidden_shape, np.float32) if self.hidden_shape else None

    def __call__(self, obs: np.ndarray) -> np.ndarray:
        feeds = {self.obs_name: obs.astype(np.float32).reshape(1, OBS_DIM)}
        if self.hidden is not None:
            feeds[self.hidden_name] = self.hidden
        outputs = self.session.run(None, feeds)
        action = outputs[0].reshape(ACTION_DIM)
        if not np.all(np.isfinite(action)):
            raise RuntimeError("Policy produced non-finite actions")
        if self.hidden is not None:
            self.hidden = outputs[1]
        return action
