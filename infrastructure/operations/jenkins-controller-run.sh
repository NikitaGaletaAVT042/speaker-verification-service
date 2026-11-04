#!/usr/bin/env bash
set -euo pipefail

root=/home/nikita/apps/jenkins-stack
image=jenkins/jenkins@sha256:74401de6a6f92e1afe1b02d1c8e3d0c33f263bfb5275dd8813f9150355f2367b

if docker container inspect speaker-jenkins >/dev/null 2>&1; then
    printf '%s\n' 'Контейнер speaker-jenkins уже существует.'
    exit 1
fi

test -d "$root/home"
test -f "$root/init/01-controller.groovy"

args=(
    run -d
    --name speaker-jenkins
    --restart unless-stopped
    --init
    --user 1000:1000
    --cpus 1
    --memory 2g
    --pids-limit 256
    --read-only
    --tmpfs /tmp:rw,size=256m,mode=1777
    --cap-drop ALL
    --security-opt no-new-privileges
    --publish 127.0.0.1:8080:8080
    --env 'JAVA_OPTS=-Xms256m -Xmx1024m -Djava.awt.headless=true'
    --env 'JENKINS_OPTS=--httpPort=8080'
    --mount "type=bind,source=$root/home,target=/var/jenkins_home"
    --mount "type=bind,source=$root/init,target=/var/jenkins_home/init.groovy.d,readonly"
    --log-driver json-file
    --log-opt max-size=10m
    --log-opt max-file=3
    "$image"
)

docker "${args[@]}"
