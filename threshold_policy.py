"""Select a validation-derived threshold for a bounded error-cost slider."""

import hashlib
import json
import math
from pathlib import Path

import numpy as np


POLICY_SHA256 = "040a563d4c2677a1e0adc00cfadff7fad7c6346d81439fbb01d4906eefaee28f"
MODEL_MANIFEST_SHA256 = "b1644516117ef26122b14636e202bcf864d113845842e49d66cd46dc5fb0d85c"


def unique_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Non-finite JSON constant")


class ThresholdPolicy:
    def __init__(self, path):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 1024**2:
            raise ValueError("Unexpected policy file")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != POLICY_SHA256:
            raise ValueError("Threshold policy SHA-256 mismatch")
        data = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=reject_constant)
        if (data.get("schema_version") != 1
                or data.get("policy_id") != "vibravox-validation-cost-policy-v1"
                or data.get("model_manifest_sha256") != MODEL_MANIFEST_SHA256
                or data.get("source_split") != "validation"
                or data.get("decision_rule") != "similarity > threshold"
                or data.get("tie_break") != "highest_threshold"
                or data.get("same_prior") != 0.5
                or data.get("scenario_weights") != {name: 1 / 3 for name in ("clean-clean", "clean-noisy", "noisy-noisy")}
                or data.get("balance_min") != -2 or data.get("balance_max") != 2):
            raise ValueError("Unexpected threshold policy metadata")
        self._thresholds = np.asarray(data["thresholds"], dtype=np.float64)
        self._far = np.asarray(data["mean_far"], dtype=np.float64)
        self._frr = np.asarray(data["mean_frr"], dtype=np.float64)
        if any(array.shape != (5152,) or not np.isfinite(array).all()
               for array in (self._thresholds, self._far, self._frr)):
            raise ValueError("Invalid threshold table")
        if (not (np.diff(self._thresholds) > 0).all()
                or not (np.diff(self._far) <= 0).all()
                or not (np.diff(self._frr) >= 0).all()
                or (np.abs(self._thresholds) > 1).any()
                or (self._far < 0).any() or (self._far > 1).any()
                or (self._frr < 0).any() or (self._frr > 1).any()
                or (self._far[0], self._frr[0], self._far[-1], self._frr[-1]) != (1, 0, 0, 1)):
            raise ValueError("Invalid threshold ordering, rates, or extreme decisions")
        for array in (self._thresholds, self._far, self._frr):
            array.setflags(write=False)

    def select(self, balance=0.0):
        if type(balance) not in (int, float) or not math.isfinite(balance) or not -2 <= balance <= 2:
            raise ValueError("Balance must be a finite number between -2 and 2")
        false_accept_cost = 10.0 ** max(balance, 0)
        false_reject_cost = 10.0 ** max(-balance, 0)
        risk = 0.5 * false_accept_cost * self._far + 0.5 * false_reject_cost * self._frr
        # Exact equality and the highest threshold are the documented tie-break.
        index = int(np.flatnonzero(risk == risk.min())[-1])
        return {
            "balance": float(balance),
            "false_accept_cost": false_accept_cost,
            "false_reject_cost": false_reject_cost,
            "threshold": float(self._thresholds[index]),
            "validation_far": float(self._far[index]),
            "validation_frr": float(self._frr[index]),
            "validation_risk": float(risk[index]),
            "assumed_same_prior": 0.5,
            "equal_scenario_weights": True,
            "policy_sha256": POLICY_SHA256,
        }
