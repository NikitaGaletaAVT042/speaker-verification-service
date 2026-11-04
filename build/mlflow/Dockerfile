FROM sv-service:k3s-v3

USER root

RUN python -m pip freeze > /opt/sv-runtime-before-mlflow.txt \
    && python -m pip install --no-cache-dir \
        --constraint /opt/sv-runtime-before-mlflow.txt \
        --upgrade-strategy only-if-needed \
        "mlflow-tracing==3.12.0" \
    && python -m pip check

USER 1000:1000
