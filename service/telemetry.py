"""Local Prometheus instrumentation with bounded, non-sensitive labels."""

import asyncio
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, Info, ProcessCollector, generate_latest

from service.mlflow_export import RESULTS, exporter_from_environment
from service.model_worker import AudioRejected, WorkerBusy, WorkerUnavailable
from threshold_policy import MODEL_MANIFEST_SHA256, POLICY_SHA256


BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 20, 60)
OUTCOMES = ("same", "different", "invalid_audio", "busy", "unavailable", "cancelled", "internal_error")


def route_label(path):
    path = path.rstrip("/")
    if path == "/v1/verify":
        return "verify"
    if path == "/v1/policy":
        return "policy"
    if path in {"/ui/gradio_api/upload", "/ui/upload"}:
        return "ui_upload"
    if path.startswith(("/ui/gradio_api/run/", "/ui/gradio_api/api/", "/ui/gradio_api/call/")):
        return "ui_predict"
    if path.startswith("/ui/gradio_api/queue/"):
        return "ui_queue"
    if path.startswith("/ui/assets/"):
        return "ui_static"
    if path == "/ui" or path.startswith("/ui/"):
        return "ui_other"
    if not path:
        return "root"
    return "other"


class Telemetry:
    def __init__(self, worker):
        self.worker = worker
        self.registry = CollectorRegistry()
        self.mlflow_events = Counter(
            "sv_mlflow_events_total",
            "MLflow export events by bounded result",
            ["result"],
            registry=self.registry,
        )
        for result in RESULTS:
            self.mlflow_events.labels(result)

        self.exporter = exporter_from_environment(
            MODEL_MANIFEST_SHA256,
            POLICY_SHA256,
            self._report_export,
        )

        Gauge(
            "sv_mlflow_enabled",
            "MLflow export configured: 1 or 0",
            registry=self.registry,
        ).set(int(self.exporter is not None))

        Gauge(
            "sv_mlflow_queue_depth",
            "Comparison events waiting for MLflow export",
            registry=self.registry,
        ).set_function(
            lambda: self.exporter.events.qsize() if self.exporter else 0
        )

        Gauge(
            "sv_mlflow_worker_alive",
            "MLflow export thread alive: 1 or 0",
            registry=self.registry,
        ).set_function(
            lambda: int(self.exporter.thread.is_alive())
            if self.exporter else 0
        )

        Gauge(
            "sv_mlflow_last_success_timestamp_seconds",
            "Last successful MLflow export Unix timestamp; zero before success",
            registry=self.registry,
        ).set_function(
            lambda: self.exporter.last_success if self.exporter else 0
        )
        self.comparisons = Counter("sv_comparisons_total", "Comparison attempts by bounded outcome",
                                   ["outcome"], registry=self.registry)
        self.comparison_seconds = Histogram("sv_comparison_duration_seconds",
                                            "Model worker round-trip time, including audio preparation",
                                            ["outcome"], buckets=BUCKETS, registry=self.registry)
        self.in_progress = Gauge("sv_comparisons_in_progress", "Comparison calls not yet returned",
                                 registry=self.registry)
        self.startup_seconds = Gauge("sv_model_startup_seconds", "Last successful model process startup duration",
                                     registry=self.registry)
        self.ready = Gauge("sv_model_ready", "Model process ready: 1 or 0", registry=self.registry)
        self.ready.set_function(lambda: int(worker.ready))
        self.busy = Gauge("sv_model_busy", "Model worker currently occupied: 1 or 0", registry=self.registry)
        self.busy.set_function(lambda: int(bool(getattr(worker, "_busy", False))))
        self.http_requests = Counter("sv_http_requests_total", "HTTP responses excluding probes and scrapes",
                                     ["route", "method", "status"], registry=self.registry)
        self.http_seconds = Histogram("sv_http_request_duration_seconds", "HTTP duration through response completion",
                                      ["route", "method"], buckets=BUCKETS, registry=self.registry)
        self.http_in_progress = Gauge("sv_http_requests_in_progress", "HTTP requests currently in progress",
                                      registry=self.registry)
        for outcome in OUTCOMES:
            self.comparisons.labels(outcome)
            self.comparison_seconds.labels(outcome)
        Info("sv_release", "Pinned model and operating-policy identities", registry=self.registry).info({
            "model_manifest_sha256": MODEL_MANIFEST_SHA256, "policy_sha256": POLICY_SHA256,
        })
        ProcessCollector(namespace="sv_api", registry=self.registry)
        ProcessCollector(namespace="sv_model", pid=self._model_pid, registry=self.registry)

    def _report_export(self, result):
        self.mlflow_events.labels(result).inc()

    def start_exporter(self):
        if self.exporter is not None:
            self.exporter.start()

    async def close_exporter(self):
        if self.exporter is not None:
            await asyncio.to_thread(self.exporter.close)

    def _model_pid(self):
        process = getattr(self.worker, "_process", None)
        return process.pid if process is not None and self.worker.ready else 0

    async def compare(self, first, second, balance):
        started_ns = time.time_ns()
        started = time.monotonic()
        outcome = "internal_error"
        self.in_progress.inc()
        try:
            result = await self.worker.compare(first, second, balance)
            outcome = {"SAME": "same", "DIFFERENT": "different"}.get(result.get("decision"), "internal_error")
            return result
        except AudioRejected:
            outcome = "invalid_audio"
            raise
        except WorkerBusy:
            outcome = "busy"
            raise
        except WorkerUnavailable:
            outcome = "unavailable"
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            elapsed = time.monotonic() - started
            self.comparisons.labels(outcome).inc()
            self.comparison_seconds.labels(outcome).observe(elapsed)
            self.in_progress.dec()
            if self.exporter is not None:
                self.exporter.offer(outcome, elapsed, balance, started_ns)

    def render(self):
        return generate_latest(self.registry)


class HTTPMetrics:
    def __init__(self, app, telemetry):
        self.app = app
        self.telemetry = telemetry

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") in {
                "/metrics", "/health/live", "/health/ready"}:
            await self.app(scope, receive, send)
            return
        route = route_label(scope["path"])
        method = scope["method"] if scope["method"] in {
            "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER"
        started = time.monotonic()
        status = "500"
        self.telemetry.http_in_progress.inc()

        async def observed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                value = message["status"]
                status = str(value) if value in {
                    200, 201, 202, 204, 206, 301, 302, 303, 304, 307, 308,
                    400, 401, 403, 404, 405, 408, 413, 415, 422, 429, 499,
                    500, 502, 503, 504} else "other"
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except asyncio.CancelledError:
            status = "499"
            raise
        finally:
            self.telemetry.http_requests.labels(route, method, status).inc()
            self.telemetry.http_seconds.labels(route, method).observe(time.monotonic() - started)
            self.telemetry.http_in_progress.dec()
