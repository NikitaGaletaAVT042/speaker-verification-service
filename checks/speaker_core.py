"""CPU speaker-verification core for a pinned, trusted ECAPA release."""

import hashlib
import json
import math
import os
from pathlib import Path
import stat

import numpy as np
import soundfile as sf
import torch
import torch.nn.functional as functional
import torchaudio
from speechbrain.lobes.features import Fbank
from speechbrain.lobes.models.ECAPA_TDNN import ECAPA_TDNN
from speechbrain.processing.features import InputNormalization


MANIFEST_SHA256 = "b1644516117ef26122b14636e202bcf864d113845842e49d66cd46dc5fb0d85c"
RELEASE_ID = "ecapa-vibravox-20261103T151003849219Z"
EXPECTED_FILES = {
    "checkpoint/best.pt", "config/calibration.json", "config/embedding_config.json",
    "base_model/hyperparams.yaml", "base_model/embedding_model.ckpt",
    "base_model/mean_var_norm_emb.ckpt", "base_model/classifier.ckpt",
    "base_model/label_encoder.ckpt",
}
MAX_FILE_BYTES = 5 * 1024**2


class InvalidAudio(ValueError):
    """An audio file failed bounded input validation."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Non-finite JSON constant")


def read_json(path, maximum=65536):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ValueError("Unexpected metadata file")
    return json.loads(path.read_bytes(), object_pairs_hook=unique_keys, parse_constant=reject_constant)


def verify_release(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Unexpected release directory")
    root = root.resolve(strict=True)
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path)
    if sha256_file(manifest_path) != MANIFEST_SHA256:
        raise ValueError("Manifest SHA-256 mismatch")
    if (manifest.get("schema_version") != 1 or manifest.get("release_id") != RELEASE_ID
            or manifest.get("checkpoint_step") != 500
            or set(manifest.get("files", {})) != EXPECTED_FILES):
        raise ValueError("Unexpected release metadata")
    for relative in EXPECTED_FILES:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError("Unexpected release file")
        path.resolve(strict=True).relative_to(root)
        record = manifest["files"][relative]
        if (not 0 < path.stat().st_size <= 128 * 1024**2
                or path.stat().st_size != record["size_bytes"]
                or sha256_file(path) != record["sha256"]):
            raise ValueError("Artifact integrity failure")
    calibration = read_json(root / "config/calibration.json")
    embedding = read_json(root / "config/embedding_config.json")
    for field in ("a", "b", "decision_threshold"):
        value = calibration.get(field)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Invalid calibration")
    if (calibration.get("step") != 500 or calibration.get("assumed_same_prior") != 0.5
            or calibration.get("equal_scenario_weights") is not True
            or not -1 <= calibration["decision_threshold"] <= 1
            or embedding.get("step") != 500 or embedding.get("sample_rate_before_model") != 16000
            or embedding.get("embedding_dimension") != 192 or embedding.get("l2_normalized") is not True
            or embedding.get("denoising") is not False or embedding.get("pause_removal") is not False):
        raise ValueError("Unexpected inference configuration")
    return root, calibration


def load_audio(path):
    """Read an internal file path once; public callers must supply uploads, not paths."""
    try:
        with Path(path).open("rb") as raw:
            before = os.fstat(raw.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_FILE_BYTES:
                raise InvalidAudio("Audio file size or type is invalid")
            with sf.SoundFile(raw, mode="r") as audio:
                rate, channels, frames = audio.samplerate, audio.channels, audio.frames
                if audio.format not in {"WAV", "WAVEX", "RF64"}:
                    raise InvalidAudio("Only WAV audio is supported")
                if channels not in (1, 2) or not 8000 <= rate <= 48000:
                    raise InvalidAudio("Unsupported audio channels or sample rate")
                if not rate <= frames <= 5 * rate:
                    raise InvalidAudio("Audio duration must be between 1 and 5 seconds")
                signal = audio.read(frames=frames, dtype="float32", always_2d=True)
            after = os.fstat(raw.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise InvalidAudio("Audio file changed while being read")
    except (OSError, sf.LibsndfileError) as error:
        raise InvalidAudio("Audio file could not be read") from error
    if signal.shape != (frames, channels) or not np.isfinite(signal).all():
        raise InvalidAudio("Invalid audio samples")
    mono = signal.mean(axis=1)
    if np.max(np.abs(mono)) < 1e-6:
        raise InvalidAudio("Audio signal is too weak")
    waveform = torch.from_numpy(mono.copy())
    if rate != 8000:
        waveform = torchaudio.functional.resample(waveform, rate, 8000)
    waveform = torchaudio.functional.resample(waveform, 8000, 16000)
    if not torch.isfinite(waveform).all().item():
        raise InvalidAudio("Invalid resampled audio")
    return waveform.unsqueeze(0), frames / rate


class ECAPAVerifier:
    """One frozen CPU model; calls must be serialized by the owning worker."""

    def __init__(self, release):
        root, self.calibration = verify_release(release)
        checkpoint = torch.load(root / "checkpoint/best.pt", map_location="cpu", weights_only=True)
        if not isinstance(checkpoint, dict) or checkpoint.get("step") != 500:
            raise ValueError("Unexpected checkpoint")
        state = checkpoint.get("encoder_state")
        if not isinstance(state, dict) or not state:
            raise ValueError("Missing encoder state")
        for name, tensor in state.items():
            if (not isinstance(name, str) or not isinstance(tensor, torch.Tensor)
                    or tensor.device.type != "cpu" or not torch.isfinite(tensor).all().item()):
                raise ValueError("Invalid encoder tensor")
        self.features = Fbank(n_mels=80).eval().requires_grad_(False)
        self.normalization = InputNormalization(norm_type="sentence", std_norm=False).eval()
        self.encoder = ECAPA_TDNN(
            input_size=80, channels=[1024, 1024, 1024, 1024, 3072],
            kernel_sizes=[5, 3, 3, 3, 1], dilations=[1, 2, 3, 4, 1],
            attention_channels=128, lin_neurons=192,
        )
        self.encoder.load_state_dict(state, strict=True)
        self.encoder.eval().requires_grad_(False)

    @torch.inference_mode()
    def extract_embedding(self, waveform):
        if (not isinstance(waveform, torch.Tensor) or waveform.dtype != torch.float32
                or waveform.device.type != "cpu" or waveform.ndim != 2 or waveform.shape[0] != 1
                or not 16000 <= waveform.shape[1] <= 80000
                or not torch.isfinite(waveform).all().item()):
            raise InvalidAudio("Invalid prepared waveform")
        lengths = torch.ones(1)
        acoustic = self.normalization(self.features(waveform), lengths)
        vector = self.encoder(acoustic, lengths).reshape(-1)
        if (vector.shape != (192,) or not torch.isfinite(vector).all().item()
                or torch.linalg.vector_norm(vector).item() <= 1e-12):
            raise RuntimeError("Invalid model output")
        return functional.normalize(vector, dim=0)

    def compare_embeddings(self, first, second):
        for vector in (first, second):
            if (vector.shape != (192,) or not torch.isfinite(vector).all().item()
                    or abs(torch.linalg.vector_norm(vector).item() - 1.0) > 1e-5):
                raise RuntimeError("Invalid comparison embedding")
        score = max(-1.0, min(1.0, torch.dot(first, second).item()))
        threshold = self.calibration["decision_threshold"]
        same = score > threshold
        logit = self.calibration["a"] * score + self.calibration["b"]
        if logit >= 0:
            probability = 1 / (1 + math.exp(-logit))
        else:
            exponential = math.exp(logit)
            probability = exponential / (1 + exponential)
        return {
            "decision": "SAME" if same else "DIFFERENT",
            "similarity": score, "threshold": threshold,
            "confidence": probability if same else 1 - probability,
        }
