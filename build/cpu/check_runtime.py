from importlib.metadata import version

import numpy as np
import soundfile as sf
import torch
import torchaudio

from hyperpyyaml import load_hyperpyyaml
from speechbrain.lobes.features import Fbank
from speechbrain.lobes.models.ECAPA_TDNN import ECAPA_TDNN

torch.set_num_threads(1)
torch.set_num_interop_threads(1)

assert torch.__version__.split("+")[0] == "2.6.0"
assert torchaudio.__version__.split("+")[0] == "2.6.0"
assert torch.version.cuda is None
assert not torch.cuda.is_available()
assert "WAV" in sf.available_formats()

waveform = torch.zeros(1, 8000, dtype=torch.float32)
resampled = torchaudio.functional.resample(waveform, 8000, 16000)

assert resampled.shape == (1, 16000)
assert torch.isfinite(resampled).all().item()

features = Fbank(n_mels=80)(resampled)
assert features.ndim == 3 and features.shape[-1] == 80
assert torch.isfinite(features).all().item()

for name in (
    "torch", "torchaudio", "numpy", "scipy",
    "speechbrain", "soundfile", "hyperpyyaml",
    "huggingface-hub",
):
    print(f"{name}: {version(name)}")

print("CPU-окружение: OK")
print("WAV-библиотека, ресемплинг и Fbank: OK")
print("Класс ECAPA импортирован.")
print("Веса не загружались. Аудиофайлы не обрабатывались.")
