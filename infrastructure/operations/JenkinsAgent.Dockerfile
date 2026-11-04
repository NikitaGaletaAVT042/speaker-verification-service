FROM jenkins/inbound-agent@sha256:5069347cd3e72e5924e54c04fceb67209b41c8d0a3ab1c2b3f740e5fb2fab92a

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends podman \
    && rm -rf /var/lib/apt/lists/*

RUN groupmod --gid 1001 jenkins \
    && usermod --uid 1001 --gid 1001 jenkins \
    && mkdir -p /home/jenkins/.config/containers \
                /home/jenkins/.local/share/containers \
                /home/jenkins/.cache \
    && chown -R 1001:1001 /home/jenkins

USER 1001:1001
