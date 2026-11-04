"""Isolated checks: actual counters, real WAV inference, and shared UI accounting."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from service.app import create_app
from service.model_worker import AudioRejected, WorkerBusy, WorkerUnavailable
from service.telemetry import Telemetry, route_label


class TestStore:
    def verify(self, username, password):
        return username == "test-user" and password == "test-password-only"


def samples(text):
    return [sample for family in text_string_to_metric_families(text) for sample in family.samples]


def value(text, name, **labels):
    matched = [sample.value for sample in samples(text)
               if sample.name == name and sample.labels == labels]
    if len(matched) != 1:
        raise AssertionError(f"Expected exactly one sample: {name} {labels}")
    return matched[0]


async def check_failures():
    for error, outcome in ((AudioRejected("test"), "invalid_audio"),
                           (WorkerBusy("test"), "busy"),
                           (WorkerUnavailable("test"), "unavailable"),
                           (RuntimeError("test"), "internal_error"),
                           (asyncio.CancelledError(), "cancelled")):
        class RaisingWorker:
            ready = True
            _busy = False

            async def compare(self, first, second, balance):
                raise error

        telemetry = Telemetry(RaisingWorker())
        try:
            await telemetry.compare("first", "second", 0)
        except BaseException as caught:
            if caught is not error:
                raise AssertionError("Instrumentation changed the exception")
        else:
            raise AssertionError("Instrumentation suppressed an exception")
        text = telemetry.render().decode()
        if value(text, "sv_comparisons_total", outcome=outcome) != 1:
            raise AssertionError("Missing failure counter")
        if value(text, "sv_comparison_duration_seconds_count", outcome=outcome) != 1:
            raise AssertionError("Missing failure duration")
        if value(text, "sv_comparisons_in_progress") != 0:
            raise AssertionError("In-flight gauge leaked after failure")
        print("OK: exception preserved and counted:", outcome)


def main():
    asyncio.run(check_failures())
    if len({route_label(f"/random-input/{number}") for number in range(1000)}) != 1:
        raise AssertionError("Unbounded unknown-route labels")
    fixtures = Path("/checks/fixtures")
    manifest = json.loads((fixtures / "manifest.json").read_text())
    auth = ("test-user", "test-password-only")
    with TestClient(create_app(store=TestStore())) as client:
        if client.get("/metrics").status_code != 401:
            raise AssertionError("Metrics are not protected")

        def scrape():
            response = client.get("/metrics", auth=auth)
            response.raise_for_status()
            if "text/plain" not in response.headers["content-type"]:
                raise AssertionError("Invalid metrics content type")
            return response.text

        initial = scrape()
        if value(initial, "sv_model_ready") != 1 or value(initial, "sv_model_busy") != 0:
            raise AssertionError("Unexpected model state")
        for name in ("sv_api_process_resident_memory_bytes", "sv_model_process_resident_memory_bytes",
                     "sv_api_process_cpu_seconds_total", "sv_model_process_cpu_seconds_total"):
            if value(initial, name) < 0:
                raise AssertionError("Invalid process metric")
        if value(initial, "sv_model_startup_seconds") <= 0:
            raise AssertionError("Missing startup duration")

        if client.post("/v1/verify").status_code != 401:
            raise AssertionError("Anonymous inference is allowed")
        response = client.post("/v1/verify", auth=auth, files={
            "first": ("one.wav", b"invalid"), "second": ("two.wav", b"invalid")})
        if response.status_code != 422:
            raise AssertionError("Invalid WAV was accepted")
        for pair in manifest["pairs"]:
            first = fixtures / manifest["records"][pair["first"]]["file"]
            second = fixtures / manifest["records"][pair["second"]]["file"]
            with first.open("rb") as one, second.open("rb") as two:
                response = client.post("/v1/verify", auth=auth,
                                       files={"first": ("first.wav", one), "second": ("second.wav", two)})
            response.raise_for_status()
            result = response.json()
            if (result["decision"] != pair["reference_decision"]
                    or abs(result["similarity"] - pair["reference_similarity"]) > 5e-4):
                raise AssertionError("Inference regression")
        after_api = scrape()
        for outcome in ("same", "different"):
            if value(after_api, "sv_comparisons_total", outcome=outcome) != 3:
                raise AssertionError("API comparisons not counted exactly once")
        if value(after_api, "sv_comparisons_total", outcome="invalid_audio") != 1:
            raise AssertionError("Invalid audio not counted")
        if value(after_api, "sv_http_requests_total", route="verify", method="POST", status="401") != 1:
            raise AssertionError("HTTP authorization failure not counted")

        config = client.get("/ui/config", auth=auth).json()
        functions = [dependency for dependency in config["dependencies"] if len(dependency.get("inputs", [])) == 3]
        if len(functions) != 1:
            raise AssertionError("Comparison callback is ambiguous")
        pair = manifest["pairs"][0]
        first = fixtures / manifest["records"][pair["first"]]["file"]
        second = fixtures / manifest["records"][pair["second"]]["file"]
        with first.open("rb") as one, second.open("rb") as two:
            response = client.post("/ui/gradio_api/upload", auth=auth,
                                   files=[("files", ("first.wav", one)), ("files", ("second.wav", two))])
        response.raise_for_status()
        paths = response.json()
        data = [{"path": path, "orig_name": Path(path).name,
                 "meta": {"_type": "gradio.FileData"}} for path in paths]
        response = client.post(f"/ui/gradio_api/run/{functions[0]['api_name']}", auth=auth,
                               json={"data": [*data, 0], "session_hash": uuid4().hex})
        response.raise_for_status()
        if response.json()["data"][0]["decision"] != "SAME":
            raise AssertionError("Gradio regression")
        after_ui = scrape()
        if value(after_ui, "sv_comparisons_total", outcome="same") != 4:
            raise AssertionError("Gradio comparison was missing or counted twice")
        for outcome in ("same", "different", "invalid_audio"):
            if value(after_ui, "sv_comparison_duration_seconds_count", outcome=outcome) != value(
                    after_ui, "sv_comparisons_total", outcome=outcome):
                raise AssertionError("Duration count differs from attempt count")
        if value(after_ui, "sv_comparisons_in_progress") != 0:
            raise AssertionError("In-flight comparisons did not return to zero")
        for sample in samples(after_ui):
            if set(sample.labels) & {"username", "user", "path", "filename", "request_id", "balance", "score", "confidence"}:
                raise AssertionError("Sensitive or unbounded metric label")
        if "test-password-only" in after_ui or "/tmp/sv-jobs" in after_ui:
            raise AssertionError("Sensitive content in metrics")
        if list(Path("/tmp/sv-jobs").iterdir()):
            raise AssertionError("Job files were not cleaned")
        print("OK: six API pairs and one Gradio pair counted exactly once")
        print("OK: CPU/RAM of API and model processes; startup and comparison durations")
        print("OK: metrics require authentication; labels are bounded")
    print("Metrics integration: PASSED")


if __name__ == "__main__":
    main()
