FROM sv-cpu-runtime:frozen-7bd00af275aa

USER root

COPY requirements-web.txt /opt/requirements-web.txt

RUN python -m pip install --no-cache-dir \
        --only-binary=:all: \
        --constraint /opt/runtime-lock.txt \
        --requirement /opt/requirements-web.txt \
    && python -m pip check \
    && python -m pip freeze > /opt/web-runtime-lock.txt

ENV GRADIO_ANALYTICS_ENABLED=False \
    GRADIO_TEMP_DIR=/tmp/gradio \
    DO_NOT_TRACK=1

USER 10001:10001
