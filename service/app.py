"""Authenticated Gradio and HTTP interface sharing one model process."""

from contextlib import asynccontextmanager
import asyncio
import os
from pathlib import Path
import stat
import tempfile
import time
from uuid import uuid4

import gradio as gr
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST
from starlette.datastructures import UploadFile

from service.auth import PasswordStore
from service.http_auth import BasicAuthMiddleware
from service.metrics_access import MetricsAccess
from service.model_worker import AudioRejected, ModelWorker, WorkerBusy, WorkerUnavailable
from service.request_limits import RequestLimits
from service.telemetry import HTTPMetrics, Telemetry
from threshold_policy import ThresholdPolicy


MAX_FILE_BYTES = 5 * 1024**2


def copy_cached_audio(source, destination, cache_root):
    path = Path(source)
    if path.is_symlink():
        raise HTTPException(422, "Invalid audio upload")
    try:
        path.resolve(strict=True).relative_to(cache_root)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as reader:
            info = os.fstat(reader.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE_BYTES:
                raise HTTPException(422, "Audio must be between 1 byte and 5 MiB")
            with destination.open("xb") as writer:
                total = 0
                while chunk := reader.read(65536):
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise HTTPException(413, "Audio exceeds 5 MiB")
                    writer.write(chunk)
    except (OSError, ValueError):
        raise HTTPException(422, "Invalid audio upload") from None


def create_app(store=None, worker_factory=ModelWorker):
    policy_path = Path(os.environ.get("SV_POLICY_PATH", "/app/threshold-policy-v1.json"))
    policy = ThresholdPolicy(policy_path)
    if store is None:
        store = PasswordStore(Path(os.environ.get("SV_USERS_PATH", "/run/secrets/app-users.json")))
    jobs_root = Path(os.environ.get("SV_JOBS_PATH", "/tmp/sv-jobs"))
    jobs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache_root = Path(os.environ.get("GRADIO_TEMP_DIR", "/tmp/gradio"))
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache_root = cache_root.resolve(strict=True)
    worker = worker_factory(os.environ.get("SV_MODEL_PATH", "/model"), policy_path, jobs_root)
    telemetry = Telemetry(worker)

    @asynccontextmanager
    async def lifespan(app):
        try:
            started = time.monotonic()
            await worker.start()
            telemetry.startup_seconds.set(time.monotonic() - started)
            telemetry.start_exporter()
            yield
        finally:
            try:
                await telemetry.close_exporter()
            finally:
                await worker.close()

    app = FastAPI(title="Speaker verification", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", include_in_schema=False)
    async def index():
        return RedirectResponse("/ui/")

    @app.get("/health/live", include_in_schema=False)
    async def live():
        # The local model process is an essential part of this instance.
        # Its permanent failure should allow the supervisor to restart us.
        return JSONResponse({"status": "alive" if worker.ready else "failed"},
                            status_code=200 if worker.ready else 503)

    @app.get("/health/ready", include_in_schema=False)
    async def ready():
        return JSONResponse({"status": "ready" if worker.ready else "not_ready"},
                            status_code=200 if worker.ready else 503)

    @app.get("/v1/policy")
    async def get_policy(balance: float = Query(default=0.0, ge=-2.0, le=2.0)):
        return policy.select(balance)

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        content = await asyncio.to_thread(telemetry.render)
        return Response(content, headers={"Content-Type": CONTENT_TYPE_LATEST, "Cache-Control": "no-store"})

    async def compare(first, second, balance):
        try:
            result = await telemetry.compare(first, second, balance)
        except AudioRejected:
            raise HTTPException(422, "Invalid WAV audio or comparison settings") from None
        except WorkerBusy:
            raise HTTPException(429, "Model is busy", headers={"Retry-After": "1"}) from None
        except WorkerUnavailable:
            raise HTTPException(503, "Model is unavailable") from None
        result["request_id"] = uuid4().hex
        return result

    @app.post("/v1/verify")
    async def verify(request: Request):
        try:
            async with request.form(max_files=2, max_fields=1) as form:
                items = list(form.multi_items())
                keys = [key for key, value in items]
                if (keys.count("first") != 1 or keys.count("second") != 1
                        or keys.count("balance") > 1 or set(keys) - {"first", "second", "balance"}):
                    raise HTTPException(422, "Expected first, second and optional balance")
                first, second = form["first"], form["second"]
                if not isinstance(first, UploadFile) or not isinstance(second, UploadFile):
                    raise HTTPException(422, "Two WAV uploads are required")
                try:
                    balance = float(form.get("balance", "0"))
                    policy.select(balance)
                except (ValueError, TypeError):
                    raise HTTPException(422, "Balance must be between -2 and 2") from None
                with tempfile.TemporaryDirectory(prefix="job-", dir=jobs_root) as directory:
                    paths = []
                    for number, uploaded in enumerate((first, second)):
                        path = Path(directory) / f"audio-{number}.wav"
                        total = 0
                        with path.open("xb") as writer:
                            while chunk := await uploaded.read(65536):
                                total += len(chunk)
                                if total > MAX_FILE_BYTES:
                                    raise HTTPException(413, "Audio exceeds 5 MiB")
                                writer.write(chunk)
                        if total == 0:
                            raise HTTPException(422, "Audio is empty")
                        paths.append(path)
                    return await compare(*paths, balance)
        except OSError:
            raise HTTPException(503, "Temporary storage unavailable") from None

    def describe(balance):
        selected = policy.select(balance)
        return (
            f"**Цена ложного совпадения:** {selected['false_accept_cost']:.2f}\n\n"
            f"**Цена ложного отказа:** {selected['false_reject_cost']:.2f}\n\n"
            f"**Порог:** {selected['threshold']:.6f}\n\n"
            f"**Validation FAR:** {100 * selected['validation_far']:.2f}% · "
            f"**Validation FRR:** {100 * selected['validation_frr']:.2f}%\n\n"
            "Предположение: SAME/DIFFERENT — 50/50; три сценария имеют одинаковый вес."
        )

    async def verify_ui(first, second, balance):
        try:
            policy.select(balance)
            if not first or not second:
                raise HTTPException(422, "Upload two WAV files")
            with tempfile.TemporaryDirectory(prefix="job-", dir=jobs_root) as directory:
                paths = [Path(directory) / f"audio-{number}.wav" for number in range(2)]
                for source, destination in zip((first, second), paths):
                    copy_cached_audio(source, destination, cache_root)
                return await compare(*paths, balance)
        except HTTPException as error:
            raise gr.Error(error.detail) from None
        except ValueError:
            raise gr.Error("Balance must be between -2 and 2") from None
        except OSError:
            raise gr.Error("Temporary storage unavailable") from None

    def authenticated_user(request: Request):
        return getattr(request.state, "username", None)

    with gr.Blocks(analytics_enabled=False, delete_cache=(60, 300)) as interface:
        gr.Markdown("# Верификация диктора")
        gr.Markdown("Два WAV-файла: mono/stereo, 8–48 кГц, 1–5 секунд, до 5 МиБ каждый.")
        with gr.Row():
            first = gr.File(label="Первая запись", file_types=[".wav"], type="filepath")
            second = gr.File(label="Вторая запись", file_types=[".wav"], type="filepath")
        balance = gr.Slider(minimum=-2, maximum=2, value=0, step=0.1,
                            label="Баланс цены ошибок",
                            info="Влево — дороже ложный отказ. Вправо — дороже ложное совпадение.")
        description = gr.Markdown(describe(0.0))
        button = gr.Button("Сравнить")
        result = gr.JSON(label="Результат")
        balance.change(describe, inputs=balance, outputs=description, queue=False)
        button.click(verify_ui, inputs=[first, second, balance], outputs=result, queue=False)

    app = gr.mount_gradio_app(app, interface, path="/ui", auth_dependency=authenticated_user,
                             allowed_paths=[],
                             blocked_paths=[str(jobs_root), "/model", "/app", "/checks",
                                            "/run", "/etc", "/proc", "/sys", "/dev"],
                             max_file_size=MAX_FILE_BYTES, show_error=False)
    origins = os.environ.get("SV_ALLOWED_ORIGINS", "http://127.0.0.1:7860,http://localhost:7860").split(",")
    app = RequestLimits(app, allowed_origins=origins, cache_root=cache_root)
    authenticated = BasicAuthMiddleware(app, store, public_paths={"/health/live", "/health/ready"})
    protected = MetricsAccess(authenticated, app, Path(os.environ.get(
        "SV_METRICS_TOKEN_SHA256_PATH", "/app/service/metrics-token.sha256")))
    return HTTPMetrics(protected, telemetry)
