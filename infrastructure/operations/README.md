# Эксплуатация и восстановление

## Назначение файлов

- speaker-release-check.py проверяет архив релиза и сохраняет root-копию.
- speaker-deploy.py импортирует образ, обновляет только контейнер api и
  возвращает предыдущий образ при неготовности приложения.
- speaker-release-request.py принимает через stdin только check/deploy
  и номер сборки.
- speaker-release.sudoers разрешает пользователю speaker-ci запускать
  только speaker-release-request без аргументов.
- Остальные файлы описывают запуск контроллера и агента Jenkins.

Это копии серверных файлов. Изменение файлов в Git не устанавливает
их автоматически на сервер.

## Рабочая схема

GitHub main → Jenkins speaker-ci → rootless Podman → проверки среды
и телеметрии → реальный инференс → OCI-архив → ограниченный SSH-вызов
→ проверка архива → импорт в K3s → rollout и readiness.

Рабочий образ закрепляется по manifest digest.
Сборки на контроллере Jenkins отключены.
Автозапуск проверяет Git каждые две минуты.

## Что хранится вне Git

- Приватные SSH-ключи, authorized_keys и known_hosts.
- Секрет подключения агента Jenkins.
- Пароли, app-users.json, токены SMTP/MLflow/Prometheus.
- Административный kubeconfig и состояние K3s.
- Jenkins home, данные PostgreSQL и MinIO.
- Модель, WAV и архивы релизов.

Их нельзя восстановить только из этого репозитория.
При утрате секретов нужно восстановить их из защищённой копии либо
перевыпустить и обновить соответствующие подключения.

## Пути текущего сервера

- Репозиторий: /home/nikita/apps/speaker-verification-git-stage
- Jenkins home: /home/nikita/apps/jenkins-stack/home
- Пользователь CI: speaker-ci, UID/GID 1001
- Рабочий каталог агента: /home/speaker-ci/agent
- Rootless Podman socket: /run/user/1001/podman/podman.sock
- Тестовая модель и WAV: /opt/speaker-ci-inputs-v1
- Root-копии релизов и квитанции: /var/lib/speaker-releases
- Namespace и Deployment приложения: speaker-verification
- SSH сервера: порт 2222

Пути и UID необходимо проверить перед восстановлением на другом сервере.

## Установка серверных команд

Выполнять из корня проверенного checkout под администратором.
Сначала сохранить существующие серверные команды для отката.

```bash
sudo install -o root -g root -m 700 infrastructure/operations/speaker-release-check.py /usr/local/sbin/speaker-release-check
sudo install -o root -g root -m 700 infrastructure/operations/speaker-deploy.py /usr/local/sbin/speaker-deploy
sudo install -o root -g root -m 700 infrastructure/operations/speaker-release-request.py /usr/local/sbin/speaker-release-request
sudo visudo -cf infrastructure/operations/speaker-release.sudoers
```

Только после успешной проверки sudoers:

```bash
sudo install -o root -g root -m 440 infrastructure/operations/speaker-release.sudoers /etc/sudoers.d/speaker-release
sudo visudo -c
```

## Последовательность восстановления

1. Отключить запуск заданий Jenkins на время восстановления.
2. Восстановить K3s и его секреты из согласованной копии:
   SQLite, server token, encryption configuration и необходимые TLS-файлы.
   Сначала использовать соответствующую копии версию K3s.
3. Восстановить модель по доверенному манифесту, конфигурацию Nginx,
   сертификаты, PostgreSQL и MinIO. Не импортировать дампы в рабочие
   базы поверх существующих данных без отдельного плана.
4. Восстановить Jenkins home из копии, если он в ней присутствует.
   Если его копии нет, заново настроить контроллер, администратора,
   плагины, узел и Pipeline from SCM с Script Path Jenkinsfile.
5. Проверить пользователя CI, subordinate UID/GID ranges, linger
   и пользовательский podman.socket. Восстановить отдельные секреты.
6. Установить серверные команды, восстановить конфигурации запуска
   и ограниченный SSH-ключ. Не давать агенту административный kubeconfig
   или Docker socket.
7. Восстановить выбранный проверенный образ в containerd K3s.
   Манифесты в Git могут содержать прежний тег: перед применением
   сверить образ с квитанцией выбранного релиза.
8. Запустить контроллер и агент, проверить подключение и dry-run
   существующего релиза перед разрешением реального деплоя.
9. Проверить API readiness, HTTPS-авторизацию, сравнение через UI,
   доставку в MLflow и Prometheus, затем разрешить задания Jenkins.

При наличии контейнера с тем же именем run-скрипты откажутся создавать
дубликат. Не удалять существующий контейнер или его данные вслепую.

## Проверка после восстановления

- /health/ready через локальный Service отвечает ready.
- Публичный /v1/policy без авторизации возвращает 401.
- После сравнения растёт sv_mlflow_events_total{result="delivered"}.
- Prometheus up и sv_model_ready равны 1.
- Jenkins выполняет Pipeline из Git на speaker-builder.
- Тестовая копия подтверждает автоматический откат.

Откат проверен на отдельном Deployment с намеренно неработающим образом.
Полное восстановление K3s на отдельной машине ещё не испытывалось.
Наличие старого архива не означает, что он содержит более поздние
настройки Jenkins и CI/CD: состав копии нужно проверять.# Эксплуатация и восстановление
