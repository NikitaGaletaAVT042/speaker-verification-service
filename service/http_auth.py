import asyncio
import base64
import binascii
import hashlib
import time
from collections import OrderedDict


class BasicAuthMiddleware:
    """Общая авторизация для HTTP; проверка пароля вне event loop."""

    def __init__(self, app, store, public_paths=()):
        self.app = app
        self.store = store
        self.public_paths = frozenset(public_paths)
        self._active = 0
        self._cache = OrderedDict()

    async def _reject(self, send, status):
        messages = {
            401: b"Authentication required",
            429: b"Authentication busy",
            403: b"Forbidden",
        }
        headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"cache-control", b"no-store"),
        ]
        if status == 401:
            headers.append((
                b"www-authenticate",
                b'Basic realm="speaker-verification", charset="UTF-8"',
            ))
        if status == 429:
            headers.append((b"retry-after", b"1"))

        await send({
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        })
        await send({
            "type": "http.response.body",
            "body": messages[status],
        })

    def _finished(self, task):
        self._active -= 1
        # Исключение извлекается даже при отключении клиента.
        if not task.cancelled():
            task.exception()

    async def __call__(self, scope, receive, send):
        kind = scope["type"]

        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return

        if kind != "http":
            await self.app(scope, receive, send)
            return

        if (
            scope["path"] in self.public_paths
            and scope["method"] in {"GET", "HEAD"}
        ):
            await self.app(scope, receive, send)
            return

        values = [
            value
            for name, value in scope["headers"]
            if name.lower() == b"authorization"
        ]

        if len(values) != 1 or len(values[0]) > 2048:
            await self._reject(send, 401)
            return

        header = values[0]
        parts = header.split(b" ", 1)

        if len(parts) != 2 or parts[0].lower() != b"basic":
            await self._reject(send, 401)
            return

        try:
            decoded = base64.b64decode(parts[1], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            await self._reject(send, 401)
            return

        if not 1 <= len(username) <= 64 or not 8 <= len(password) <= 256:
            await self._reject(send, 401)
            return

        key = hashlib.sha256(header).digest()
        now = time.monotonic()
        cached = self._cache.get(key)

        if cached is not None and cached[0] > now:
            username = cached[1]
            self._cache.move_to_end(key)
        else:
            self._cache.pop(key, None)

            # Проверка и увеличение выполняются без await между ними.
            if self._active >= 2:
                await self._reject(send, 429)
                return

            self._active += 1
            task = asyncio.create_task(asyncio.to_thread(
                self.store.verify, username, password
            ))
            task.add_done_callback(self._finished)

            # Отмена HTTP-запроса не освобождает слот работающей проверки.
            valid = await asyncio.shield(task)

            if not valid:
                await self._reject(send, 401)
                return

            self._cache[key] = (time.monotonic() + 30, username)
            self._cache.move_to_end(key)

            while len(self._cache) > 128:
                self._cache.popitem(last=False)

        scope.setdefault("state", {})["username"] = username
        await self.app(scope, receive, send)
