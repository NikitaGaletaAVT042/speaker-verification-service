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
    GRADIO_ANALYTICS_ENABLED=False \
    GRADIO_TEMP_DIR=/tmp/gradio \
    DO_NOT_TRACK=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/cache \
    PYTHONPATH=/app:/checks

COPY --chmod=0444 requirements-runtime.lock.txt /opt/requirements-runtime.lock.txt

RUN python -m pip install --no-cache-dir --retries 2 \
        --only-binary=:all: \
        "torch==2.6.0+cpu" \
        "torchaudio==2.6.0+cpu" \
        --index-url https://download.pytorch.org/whl/cpu \
    && python -m pip install --no-cache-dir --retries 2 \
        --requirement /opt/requirements-runtime.lock.txt \
    && python -m pip check

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 \
        --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --chown=1000:1000 service/ /app/service/
COPY --chown=1000:1000 checks/speaker_core.py /checks/speaker_core.py
COPY --chown=1000:1000 threshold_policy.py /app/threshold_policy.py
COPY --chown=1000:1000 threshold-policy-v1.json /app/threshold-policy-v1.json

USER 1000:1000

EXPOSE 7860

ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["service.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "7860", "--workers", "1", "--limit-concurrency", "16", "--timeout-keep-alive", "5", "--timeout-graceful-shutdown", "20", "--no-proxy-headers", "--no-access-log"]
