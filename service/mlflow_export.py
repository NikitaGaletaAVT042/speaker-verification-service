"""Best-effort MLflow export outside the inference request path."""

import base64
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import queue
import threading
import time
import urllib.request


OUTCOMES = (
    "same", "different", "invalid_audio", "busy",
    "unavailable", "cancelled", "internal_error",
)

RESULTS = (
    "queued", "delivered", "failed", "full",
    "expired", "cooldown", "shutdown", "invalid",
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComparisonEvent:
    outcome: str
    duration: float
    balance: float
    started_ns: int
    queued_at: float


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MLflowExporter:
    def __init__(self, credentials, model_sha, policy_sha, report):
        if (
            credentials.get("status") != "configured"
            or credentials.get("tracking_uri")
            != "https://mlflow-taskflow.duckdns.org"
            or credentials.get("username") != "sv_telemetry"
            or not str(credentials.get("experiment_id", "")).isdigit()
            or not isinstance(credentials.get("password"), str)
            or not 16 <= len(credentials["password"]) <= 256
        ):
            raise ValueError("Invalid MLflow telemetry configuration")

        self.credentials = credentials.copy()
        self.model_sha = model_sha
        self.policy_sha = policy_sha
        self.report = report

        self.events = queue.Queue(maxsize=128)
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="mlflow-export",
            daemon=True,
        )

        self.last_success = 0.0
        self.run_id = None
        self.step = 0
        self.sdk = None

        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
        )

        auth = credentials["username"] + ":" + credentials["password"]
        self.authorization = (
            "Basic " + base64.b64encode(auth.encode()).decode()
        )

    def start(self):
        self.thread.start()

    def offer(self, outcome, duration, balance, started_ns):
        if self.stop.is_set():
            self.report("shutdown")
            return False

        if (
            outcome not in OUTCOMES
            or type(duration) not in (int, float)
            or not math.isfinite(duration)
            or duration < 0
            or type(balance) not in (int, float)
            or not math.isfinite(balance)
            or not -2 <= balance <= 2
            or type(started_ns) is not int
            or started_ns <= 0
        ):
            self.report("invalid")
            return False

        event = ComparisonEvent(
            outcome, duration, balance, started_ns, time.monotonic()
        )

        try:
            self.events.put_nowait(event)
        except queue.Full:
            self.report("full")
            return False

        self.report("queued")
        return True

    def close(self):
        self.stop.set()

        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
            self.report("shutdown")

        if self.thread.ident is not None:
            self.thread.join(timeout=2)

    def _post(self, endpoint, payload):
        request = urllib.request.Request(
            self.credentials["tracking_uri"]
            + "/api/2.0/mlflow/"
            + endpoint,
            data=json.dumps(payload, allow_nan=False).encode(),
            method="POST",
            headers={
                "Authorization": self.authorization,
                "Content-Type": "application/json",
            },
        )

        with self.opener.open(request, timeout=5) as response:
            raw = response.read(131073)

        if len(raw) > 131072:
            raise ValueError("MLflow response exceeds limit")

        return json.loads(raw) if raw else {}

    def _configure_sdk(self):
        os.environ.update({
            "MLFLOW_TRACKING_URI": self.credentials["tracking_uri"],
            "MLFLOW_TRACKING_USERNAME": self.credentials["username"],
            "MLFLOW_TRACKING_PASSWORD": self.credentials["password"],
            "MLFLOW_HTTP_REQUEST_TIMEOUT": "5",
            "MLFLOW_HTTP_REQUEST_MAX_RETRIES": "0",
            "MLFLOW_ENABLE_ASYNC_TRACE_LOGGING": "false",
        })

        import mlflow

        logging.getLogger("mlflow").setLevel(logging.CRITICAL)
        self.sdk = mlflow

    def _finish_run(self):
        if self.run_id is not None:
            self._post("runs/update", {
                "run_id": self.run_id,
                "status": "FINISHED",
                "end_time": int(time.time() * 1000),
            })
            self.run_id = None

    def _deliver(self, event):
        if self.sdk is None:
            self._configure_sdk()

        if self.run_id is None:
            tags = {
                "mlflow.runName": "production-inference",
                "purpose": "production_inference",
                "model_manifest_sha256": self.model_sha,
                "policy_sha256": self.policy_sha,
                "registered_model": "speaker-verification-ecapa",
                "model_version": "1",
            }

            result = self._post("runs/create", {
                "experiment_id": self.credentials["experiment_id"],
                "start_time": event.started_ns // 1000000,
                "tags": [
                    {"key": key, "value": value}
                    for key, value in tags.items()
                ],
            })

            self.run_id = result["run"]["info"]["run_id"]
            self.step = 0

        ended_ns = event.started_ns + int(event.duration * 1000000000)

        span = self.sdk.start_span_no_context(
            name="speaker_verification.compare",
            experiment_id=self.credentials["experiment_id"],
            start_time_ns=event.started_ns,
            tags={
                "purpose": "production_inference",
                "mlflow.sourceRun": self.run_id,
            },
            attributes={
                "outcome": event.outcome,
                "balance": event.balance,
                "duration_seconds": event.duration,
                "model_manifest_sha256": self.model_sha,
                "policy_sha256": self.policy_sha,
            },
        )

        if span.trace_id == "MLFLOW_NO_OP_SPAN_TRACE_ID":
            raise RuntimeError("MLflow did not create trace")

        span.end(
            end_time_ns=ended_ns,
            status=(
                "ERROR"
                if event.outcome in {"unavailable", "internal_error"}
                else "OK"
            ),
        )

        if self.sdk.get_trace(span.trace_id) is None:
            raise RuntimeError("MLflow trace was not persisted")

        metrics = {
            "inference.duration_seconds": event.duration,
            "inference.balance": event.balance,
        }

        metrics.update({
            "inference.outcome." + value: float(event.outcome == value)
            for value in OUTCOMES
        })

        self._post("runs/log-batch", {
            "run_id": self.run_id,
            "metrics": [
                {
                    "key": key,
                    "value": value,
                    "timestamp": ended_ns // 1000000,
                    "step": self.step,
                }
                for key, value in metrics.items()
            ],
        })

        self.step += 1
        if self.step >= 1000:
            self._finish_run()

    def _run(self):
        cooldown_until = 0.0

        try:
            while not self.stop.is_set():
                try:
                    event = self.events.get(timeout=0.5)
                except queue.Empty:
                    continue

                now = time.monotonic()

                if self.stop.is_set():
                    self.report("shutdown")
                elif now - event.queued_at > 60:
                    self.report("expired")
                elif now < cooldown_until:
                    self.report("cooldown")
                else:
                    try:
                        self._deliver(event)
                    except Exception as error:
                        # Boundary of optional, best-effort telemetry.
                        self.report("failed")
                        cooldown_until = time.monotonic() + 30
                        LOGGER.warning(
                            "MLflow export failed (%s)",
                            type(error).__name__,
                        )
                    else:
                        self.last_success = time.time()
                        self.report("delivered")
        finally:
            try:
                self._finish_run()
            except Exception as error:
                LOGGER.warning(
                    "MLflow run close failed (%s)",
                    type(error).__name__,
                )


def exporter_from_environment(model_sha, policy_sha, report):
    enabled = os.environ.get("SV_MLFLOW_ENABLED", "0")

    if enabled == "0":
        return None
    if enabled != "1":
        raise ValueError("SV_MLFLOW_ENABLED must be 0 or 1")

    path = Path(os.environ.get(
        "SV_MLFLOW_CREDENTIALS_PATH",
        "/run/mlflow/credentials.json",
    ))

    with path.open("rb") as file:
        raw = file.read(8193)

    if len(raw) > 8192:
        raise ValueError("MLflow configuration exceeds limit")

    return MLflowExporter(
        json.loads(raw), model_sha, policy_sha, report
    )
