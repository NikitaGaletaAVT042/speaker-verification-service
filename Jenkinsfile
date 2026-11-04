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
    }
}
