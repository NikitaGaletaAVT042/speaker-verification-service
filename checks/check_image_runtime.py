from importlib.metadata import version
from pathlib import Path
import os

assert os.geteuid() == 1000, "Ожидался UID 1000"

lines = Path("/opt/requirements-runtime.lock.txt").read_text().splitlines()
for line in lines:
    name, expected = line.split("==", 1)
    actual = version(name)
    if actual != expected:
        raise RuntimeError(f"{name}: {actual} != {expected}")

import mlflow
from service.app import create_app
from service.model_worker import ModelWorker

assert callable(mlflow.start_span)
assert callable(create_app)
assert callable(ModelWorker)

print(f"Версии {len(lines)} пакетов, UID и импорты: PASSED")
print("Модель не загружалась. Сетевые запросы не выполнялись.")
