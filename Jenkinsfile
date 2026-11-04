pipeline {
    agent { label 'speaker-builder' }

    options {
        timeout(time: 130, unit: 'MINUTES')
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '10'))
        skipDefaultCheckout(true)
    }

    stages {
        stage('Agent check') {
            steps {
                sh '''
                    set -eu
                    test "$(id -u)" = "1001"
                    test "$(id -g)" = "1001"
                    test ! -S /var/run/docker.sock

                    rootless=$(podman --remote info \
                        --format '{{.Host.Security.Rootless}}')
                    test "$rootless" = "true"
                '''
            }
        }

        stage('Checkout') {
            steps {
                checkout scm

                sh '''
                    set -eu
                    git log -1 --format='%h %s'
                    test -f Dockerfile
                    test -f requirements-runtime.lock.txt
                    test -f service/mlflow_export.py
                '''
            }
        }

        stage('Build image') {
            options {
                timeout(time: 120, unit: 'MINUTES')
            }

            steps {
                sh '''
                    set -eu
                    image="localhost/speaker-verification:ci-${BUILD_NUMBER}"
                    revision=$(git rev-parse HEAD)

                    podman --remote build \
                        --layers \
                        --memory=3g \
                        --cpu-period=100000 \
                        --cpu-quota=100000 \
                        --label "org.opencontainers.image.revision=$revision" \
                        --tag "$image" \
                        --file Dockerfile \
                        .

                    podman --remote image inspect "$image" \
                        --format 'Built image: {{.Id}}'
                '''
            }
        }

        stage('Image checks') {
            options {
                timeout(time: 5, unit: 'MINUTES')
            }

            steps {
                sh '''
                    set -eu
                    image="localhost/speaker-verification:ci-${BUILD_NUMBER}"

                    for check in check_image_runtime.py check_telemetry_hooks.py
                    do
                        podman --remote run --rm                             --network none                             --read-only                                                          --user 1000:1000                             --tmpfs /tmp:rw,size=64m,mode=1777                             --cpus 1                             --memory 512m                             --pids-limit 128                             --cap-drop ALL                             --security-opt no-new-privileges                             --env SV_MLFLOW_ENABLED=0                             --mount "type=bind,source=$WORKSPACE/checks/$check,target=/check.py,readonly"                             --entrypoint python                             "$image" /check.py
                    done
                '''
            }
        }


        stage('Inference regression') {
            options {
                timeout(time: 10, unit: 'MINUTES')
            }

            steps {
                sh '''
                    set -eu
                    image="localhost/speaker-verification:ci-${BUILD_NUMBER}"

                    podman --remote run --rm \
                        --name "speaker-ci-regression-${BUILD_NUMBER}" \
                        --network none \
                        --read-only \
                        --user 1000:1000 \
                        --tmpfs /tmp:rw,size=256m,mode=1777 \
                        --cpus 1 \
                        --memory 2g \
                        --pids-limit 128 \
                        --cap-drop ALL \
                        --security-opt no-new-privileges \
                        --env SV_MLFLOW_ENABLED=0 \
                        --env SV_MODEL_PATH=/model \
                        --env SV_METRICS_TOKEN_SHA256_PATH=/tmp/test-metrics-token.sha256 \
                        --env HOME=/tmp \
                        --env GRADIO_TEMP_DIR=/tmp/gradio \
                        --mount type=bind,source=/opt/speaker-ci-inputs-v1/model,target=/model,readonly \
                        --mount type=bind,source=/opt/speaker-ci-inputs-v1/fixtures,target=/checks/fixtures,readonly \
                        --mount "type=bind,source=$WORKSPACE/checks,target=/ci-checks,readonly" \
                        --entrypoint python \
                        "$image" /ci-checks/run_inference_regression.py
                '''
            }

            post {
                always {
                    sh '''
                        podman --remote rm --force --ignore \
                            "speaker-ci-regression-${BUILD_NUMBER}"
                    '''
                }
            }
        }

    }
}
