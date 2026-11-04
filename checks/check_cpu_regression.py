"""Compare server CPU inference against saved validation embeddings."""

import argparse
from pathlib import Path
import tempfile
import time

import numpy as np
import soundfile as sf
import torch

from speaker_core import ECAPAVerifier, InvalidAudio, load_audio, read_json, sha256_file, MANIFEST_SHA256


def check_invalid_audio():
    with tempfile.TemporaryDirectory(prefix="invalid-audio-") as temporary:
        root = Path(temporary)
        (root / "empty.wav").write_bytes(b"")
        (root / "corrupt.wav").write_bytes(b"not a WAV file")
        for name, frames, rate, channels in (
            ("silence.wav", 8000, 8000, 1),
            ("short.wav", 4000, 8000, 1),
            ("long.wav", 40800, 8000, 1),
            ("channels.wav", 8000, 8000, 3),
            ("sample_rate.wav", 4000, 4000, 1),
        ):
            samples = np.full((frames, channels), 0 if name == "silence.wav" else 0.01, dtype=np.float32)
            sf.write(root / name, samples, rate, subtype="PCM_16")
        samples = np.full(8000, 0.01, dtype=np.float32)
        samples[4000] = np.nan
        sf.write(root / "nan.wav", samples, 8000, subtype="FLOAT")
        with (root / "oversized.wav").open("wb") as file:
            file.truncate(5 * 1024**2 + 1)
        cases = sorted(root.glob("*.wav"))
        for path in cases:
            try:
                load_audio(path)
            except InvalidAudio:
                pass
            else:
                raise ValueError("Invalid audio was accepted")
        print(f"Invalid audio rejection: PASSED ({len(cases)} cases)", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=Path("/model"))
    parser.add_argument("--fixtures", type=Path, default=Path("/checks/fixtures"))
    args = parser.parse_args()
    fixture_root = args.fixtures.resolve(strict=True)
    manifest = read_json(fixture_root / "manifest.json", maximum=256 * 1024)
    if manifest.get("schema_version") != 1 or manifest.get("model_manifest_sha256") != MANIFEST_SHA256:
        raise ValueError("Unexpected fixture provenance")
    if len(manifest["records"]) > 12 or len(manifest["pairs"]) != 6:
        raise ValueError("Unexpected fixture count")
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    verifier = ECAPAVerifier(args.model_dir)
    vectors = {}
    max_embedding_delta = 0.0
    for key, record in manifest["records"].items():
        path = fixture_root / record["file"]
        if path.is_symlink() or not path.is_file():
            raise ValueError("Unexpected fixture file")
        path.resolve(strict=True).relative_to(fixture_root)
        if path.stat().st_size != record["size_bytes"] or sha256_file(path) != record["sha256"]:
            raise ValueError("Fixture integrity failure")
        waveform, duration = load_audio(path)
        started = time.perf_counter()
        vector = verifier.extract_embedding(waveform)
        elapsed = time.perf_counter() - started
        reference = np.asarray(record["reference_embedding"], dtype=np.float64)
        if reference.shape != (192,) or not np.isfinite(reference).all():
            raise ValueError("Invalid reference embedding")
        difference = float(np.max(np.abs(vector.numpy().astype(np.float64) - reference)))
        max_embedding_delta = max(max_embedding_delta, difference)
        if difference > 5e-4:
            raise ValueError("CPU embedding differs from the saved reference")
        vectors[key] = vector
        print(f"AUDIO {key}: {duration:.2f}s; inference={elapsed:.3f}s; embedding_delta={difference:.8f}", flush=True)

    max_score_delta = 0.0
    for pair in manifest["pairs"]:
        first, second = vectors[pair["first"]], vectors[pair["second"]]
        result = verifier.compare_embeddings(first, second)
        reverse = verifier.compare_embeddings(second, first)
        difference = abs(result["similarity"] - pair["reference_similarity"])
        confidence_delta = abs(result["confidence"] - pair["reference_confidence"])
        max_score_delta = max(max_score_delta, difference)
        if (result["decision"] != pair["reference_decision"] or result != reverse
                or difference > 5e-4 or confidence_delta > 0.003):
            raise ValueError("CPU pair result differs from the reference")
        print(
            f"PAIR {pair['scenario']} {pair['reference_decision']}: {result['decision']}; "
            f"similarity={result['similarity']:.6f}; confidence={result['confidence']:.6f}",
            flush=True,
        )
    print(f"Maximum embedding delta: {max_embedding_delta:.8f}", flush=True)
    print(f"Maximum similarity delta: {max_score_delta:.8f}", flush=True)
    check_invalid_audio()
    print("CPU regression: PASSED (6 pairs, three scenarios, symmetric decisions)", flush=True)
    print("Fixed checkpoint threshold; this is a portability test, not a new quality estimate.", flush=True)


if __name__ == "__main__":
    main()
