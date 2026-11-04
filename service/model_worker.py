import asyncio
import math
import multiprocessing as mp
from pathlib import Path
import time


class WorkerUnavailable(RuntimeError):
    pass


class WorkerBusy(RuntimeError):
    pass


class AudioRejected(ValueError):
    pass


def _serve(connection, release, policy_path, audio_root):
    # Torch импортируется только внутри процесса модели.
    try:
        import torch
        from speaker_core import ECAPAVerifier, InvalidAudio, load_audio
        from threshold_policy import ThresholdPolicy

        torch.set_num_threads(1)
        model = ECAPAVerifier(release)
        policy = ThresholdPolicy(policy_path)
        root = Path(audio_root).resolve(strict=True)
        connection.send({"status": "ready"})

        while True:
            request = connection.recv()
            if request is None:
                break

            try:
                paths = []
                for value in request["paths"]:
                    path = Path(value)
                    if path.is_symlink():
                        raise InvalidAudio("Symbolic links are not accepted")
                    path = path.resolve(strict=True)
                    path.relative_to(root)
                    paths.append(path)

                if len(paths) != 2:
                    raise InvalidAudio("Exactly two audio files are required")

                selected = policy.select(request["balance"])
                started = time.monotonic()

                first, first_seconds = load_audio(paths[0])
                second, second_seconds = load_audio(paths[1])

                first_embedding = model.extract_embedding(first)
                second_embedding = model.extract_embedding(second)

                result = model.compare_embeddings(
                    first_embedding, second_embedding
                )

                score = result["similarity"]
                same = score > selected["threshold"]
                logit = (
                    model.calibration["a"] * score
                    + model.calibration["b"]
                )

                if logit >= 0:
                    probability = 1 / (1 + math.exp(-logit))
                else:
                    exponential = math.exp(logit)
                    probability = exponential / (1 + exponential)

                result.update({
                    "decision": "SAME" if same else "DIFFERENT",
                    "threshold": selected["threshold"],
                    "confidence": probability if same else 1 - probability,
                    "balance": selected["balance"],
                    "false_accept_cost": selected["false_accept_cost"],
                    "false_reject_cost": selected["false_reject_cost"],
                    "audio_seconds": [first_seconds, second_seconds],
                    "processing_seconds": time.monotonic() - started,
                    "policy_sha256": selected["policy_sha256"],
                })

                connection.send({"status": "ok", "result": result})

            except (InvalidAudio, ValueError, OSError):
                connection.send({"status": "invalid_audio"})

    except EOFError:
        pass
    except Exception:
        # Граница процесса: не передаём пути, тензоры и traceback клиенту.
        # После внутренней ошибки процесс завершается.
        try:
            connection.send({"status": "worker_failed"})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class ModelWorker:
    """Владелец — один event loop; один запрос, без очереди ожидания."""

    def __init__(self, release, policy_path, audio_root):
        self._arguments = (
            str(release), str(policy_path), str(audio_root)
        )
        self._process = None
        self._connection = None
        self._busy = False
        self._ready = False

    @property
    def ready(self):
        return (
            self._ready
            and self._process is not None
            and self._process.is_alive()
        )

    async def _receive(self, timeout):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._connection.poll():
                return self._connection.recv()
            if not self._process.is_alive():
                raise WorkerUnavailable("Model process stopped")
            await asyncio.sleep(0.01)

        raise WorkerUnavailable("Model process deadline exceeded")

    async def start(self):
        if self._process is not None:
            raise RuntimeError("Worker has already been started")

        context = mp.get_context("spawn")
        parent, child = context.Pipe()
        self._connection = parent
        self._process = context.Process(
            target=_serve,
            args=(child, *self._arguments),
            daemon=True,
        )

        try:
            self._process.start()
            child.close()
            response = await self._receive(45)
            if response.get("status") != "ready":
                raise WorkerUnavailable("Model startup failed")
            self._ready = True
        except BaseException:
            child.close()
            await self.close()
            raise

    async def compare(self, first, second, balance=0.0):
        if not self.ready:
            raise WorkerUnavailable("Model is not ready")
        if self._busy:
            raise WorkerBusy("Model is busy")

        self._busy = True

        try:
            self._connection.send({
                "paths": [str(first), str(second)],
                "balance": balance,
            })
            response = await self._receive(15)

            if response.get("status") == "invalid_audio":
                raise AudioRejected("Audio or comparison settings rejected")

            if response.get("status") != "ok":
                raise WorkerUnavailable("Model computation failed")

            return response["result"]

        except AudioRejected:
            raise
        except asyncio.CancelledError:
            await self.close()
            raise
        except (WorkerUnavailable, EOFError, OSError):
            await self.close()
            raise WorkerUnavailable("Model process unavailable") from None
        finally:
            self._busy = False

    async def close(self):
        self._ready = False
        process = self._process

        def stop():
            if process is not None and process.pid is not None:
                if process.is_alive():
                    process.kill()
                process.join(timeout=2)
                if process.is_alive():
                    raise RuntimeError("Model process could not be stopped")
            if self._connection is not None:
                self._connection.close()

        await asyncio.to_thread(stop)
