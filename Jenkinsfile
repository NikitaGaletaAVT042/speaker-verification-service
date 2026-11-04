pipeline {
    agent { label 'speaker-builder' }

    options {
        timeout(time: 5, unit: 'MINUTES')
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
                    java -version
                    git --version
                    podman --version
                '''
            }
        }

        stage('Rootless Podman') {
            steps {
                sh '''
                    set -eu
                    rootless=$(podman --remote info \
                        --format '{{.Host.Security.Rootless}}')
                    test "$rootless" = "true"
                    podman --remote info \
                        --format 'rootless={{.Host.Security.Rootless}} storage={{.Store.GraphDriverName}} cgroups={{.Host.CgroupsVersion}}'
                '''
            }
        }

        stage('Checkout') {
            steps {
                git branch: 'main',
                    url: 'https://github.com/NikitaGaletaAVT042/speaker-verification-service.git'

                sh '''
                    set -eu
                    git log -1 --format='%h %s'
                    test -f Dockerfile
                    test -f service/mlflow_export.py
                    test -f notebooks/experiments.ipynb
                    test -f notebooks/Speaker_Verification.ipynb
                '''
            }
        }
    }
}
