FROM python@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/cache

RUN python -m pip install --no-cache-dir --retries 2 \
    --only-binary=:all: \
    torch==2.6.0 torchaudio==2.6.0 \
    --index-url https://download.pytorch.org/whl/cpu

COPY requirements-core.txt /opt/requirements-core.txt

RUN python -m pip install --no-cache-dir --retries 2 \
    --only-binary=:all: \
    -r /opt/requirements-core.txt \
    && python -m pip check \
    && python -m pip freeze > /opt/runtime-lock.txt

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY check_runtime.py /app/check_runtime.py

USER 10001:10001

CMD ["python", "/app/check_runtime.py"]
