#!/usr/bin/env bash
set -euo pipefail

if docker container inspect speaker-jenkins-agent >/dev/null 2>&1; then
    echo 'Контейнер speaker-jenkins-agent уже существует.'
    exit 1
fi

image=$(docker image inspect speaker-jenkins-agent:podman-v1 --format '{{.Id}}')

args=(
    run -d
    --name speaker-jenkins-agent
    --restart unless-stopped
    --init
    --user 1001:1001
    --cpus 1
    --memory 2g
    --pids-limit 256
    --read-only
    --tmpfs /tmp:rw,size=2g,mode=1777
    --cap-drop ALL
    --security-opt no-new-privileges
    --add-host jenkins-taskflow.duckdns.org:192.168.0.157
    --env HOME=/home/speaker-ci/agent/.home
    --env CONTAINER_HOST=unix:///run/podman/podman.sock
    --mount type=bind,source=/home/speaker-ci/deploy-client,target=/run/deploy,readonly
    --mount type=bind,source=/home/speaker-ci/agent,target=/home/speaker-ci/agent
    --mount type=bind,source=/home/speaker-ci/agent-secret,target=/run/secrets/agent-secret,readonly
    --mount type=bind,source=/run/user/1001/podman,target=/run/podman,readonly
    --log-driver json-file
    --log-opt max-size=10m
    --log-opt max-file=3
    --entrypoint java
    "$image"
    -Xms128m
    -Xmx512m
    -jar /usr/share/jenkins/agent.jar
    -url https://jenkins-taskflow.duckdns.org/
    -secret @/run/secrets/agent-secret
    -name speaker-builder
    -webSocket
    -workDir /home/speaker-ci/agent
)

docker "${args[@]}"
