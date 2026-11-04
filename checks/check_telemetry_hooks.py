import asyncio
import os
import threading
import time
from unittest.mock import patch

os.environ["SV_MLFLOW_ENABLED"] = "0"

from service.mlflow_export import MLflowExporter
from service.model_worker import AudioRejected, WorkerBusy, WorkerUnavailable
from service.telemetry import Telemetry


class Worker:
    ready = True
    _busy = False

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    async def compare(self, first, second, balance):
        if self.error is not None:
            raise self.error
        return self.result


class RecordedEvents(list):
    def qsize(self):
        return len(self)


class RecordingExporter:
    def __init__(self):
        self.events = RecordedEvents()
        self.thread = threading.Thread()
        self.last_success = 0

    def offer(self, *event):
        self.events.append(event)
        return True


async def check_outcomes():
    cases = (
        ("same", {"decision": "SAME"}, None),
        ("different", {"decision": "DIFFERENT"}, None),
        ("invalid_audio", None, AudioRejected("test")),
        ("busy", None, WorkerBusy("test")),
        ("unavailable", None, WorkerUnavailable("test")),
        ("internal_error", None, RuntimeError("test")),
        ("cancelled", None, asyncio.CancelledError()),
    )

    for outcome, result, error in cases:
        telemetry = Telemetry(Worker(result, error))
        recorder = RecordingExporter()
        telemetry.exporter = recorder

        before = time.time_ns()
        try:
            actual = await telemetry.compare("first", "second", 0)
        except BaseException as caught:
            if error is None or caught is not error:
                raise AssertionError("Изменено исключение") from caught
        else:
            assert error is None, "Исключение подавлено"
            assert actual is result, "Результат сравнения заменён"

        assert len(recorder.events) == 1
        exported_outcome, duration, balance, started_ns = recorder.events[0]
        assert exported_outcome == outcome
        assert duration >= 0
        assert balance == 0
        assert before <= started_ns <= time.time_ns()

        assert telemetry.registry.get_sample_value(
            "sv_comparisons_total", {"outcome": outcome}
        ) == 1
        assert telemetry.registry.get_sample_value(
            "sv_comparisons_in_progress"
        ) == 0

        print("Сохранены результат/исключение и учёт:", outcome)


async def check_dependency_failure():
    reports = []
    entered = threading.Event()
    release = threading.Event()

    exporter = MLflowExporter(
        {
            "status": "configured",
            "tracking_uri": "https://mlflow-taskflow.duckdns.org",
            "username": "sv_telemetry",
            "password": "synthetic-test-password-only",
            "experiment_id": "7",
        },
        "test-model",
        "test-policy",
        reports.append,
    )

    def unavailable(event):
        entered.set()
        release.wait(timeout=10)
        raise TimeoutError("synthetic dependency failure")

    result = {"decision": "SAME"}
    telemetry = Telemetry(Worker(result))
    exporter._deliver = unavailable
    telemetry.exporter = exporter

    # Даже при регрессии тест не сможет отправить HTTP-запрос.
    with patch.object(
        exporter, "_post", side_effect=AssertionError("HTTP запрещён")
    ):
        telemetry.start_exporter()
        try:
            assert await telemetry.compare("first", "second", 0) is result
            assert await asyncio.to_thread(entered.wait, 3)

            # Экспортёр занят медленной зависимостью.
            # Сравнение должно завершиться до её освобождения.
            actual = await asyncio.wait_for(
                telemetry.compare("first", "second", 0),
                timeout=1,
            )
            assert actual is result
            assert not release.is_set()
            print("Медленный MLflow не блокирует сравнение: OK")

            release.set()
            deadline = time.monotonic() + 3
            while "failed" not in reports and time.monotonic() < deadline:
                await asyncio.sleep(0.02)

            assert "failed" in reports, "Сбой экспорта не зарегистрирован"
            assert await telemetry.compare("first", "second", 0) is result
            print("Сбой экспорта не меняет результат сравнения: OK")
        finally:
            release.set()
            await telemetry.close_exporter()

    assert not exporter.thread.is_alive(), "Поток не остановился"
    print("Остановка экспортёра: OK")


async def main():
    disabled = Telemetry(Worker({"decision": "SAME"}))
    assert disabled.exporter is None
    assert disabled.registry.get_sample_value("sv_mlflow_enabled") == 0

    await check_outcomes()
    await check_dependency_failure()
    print("Telemetry hooks: PASSED")
    print("Настоящая модель и сетевые запросы не использовались.")


asyncio.run(main())
