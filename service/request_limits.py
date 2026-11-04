"""Bound authenticated upload bodies before handing them to a framework."""

import asyncio
import json
from pathlib import Path
import tempfile
import time


class RequestLimits:
    def __init__(self, app, allowed_origins, maximum=12 * 1024**2, cache_root="/tmp/gradio"):
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)
        self.maximum = maximum
        self.active = 0
        self.cache_root = Path(cache_root).resolve()

    def valid_file_paths(self, body):
        def reject_constant(value):
            raise ValueError("Non-finite JSON")

        try:
            stack = [json.loads(body, parse_constant=reject_constant)]
            visited = 0
            while stack:
                item = stack.pop()
                visited += 1
                if visited > 10000:
                    return False
                if isinstance(item, dict):
                    if "path" in item:
                        value = item["path"]
                        if not isinstance(value, str) or len(value) > 4096:
                            return False
                        path = Path(value)
                        if not path.is_absolute() or path.is_symlink():
                            return False
                        path.resolve(strict=True).relative_to(self.cache_root)
                        if not path.is_file():
                            return False
                    stack.extend(item.values())
                elif isinstance(item, list):
                    stack.extend(item)
            return True
        except (ValueError, OSError, RecursionError):
            return False

    async def reject(self, send, status, message):
        body = json.dumps({"detail": message}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path.startswith(("/ui/gradio_api/file=", "/ui/gradio_api/file/",
                            "/ui/gradio_api/stream/", "/ui/gradio_api/proxy=",
                            "/ui/file=", "/ui/file/", "/ui/stream/", "/ui/proxy=")):
            await self.reject(send, 403, "File downloads are disabled")
            return
        if scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        check_json = path.startswith("/ui/") and not path.rstrip("/").endswith("/upload")
        maximum = min(self.maximum, 1024**2) if check_json else self.maximum
        headers = scope["headers"]
        origins = [v.decode("latin-1") for k, v in headers if k.lower() == b"origin"]
        sites = [v for k, v in headers if k.lower() == b"sec-fetch-site"]
        if (len(origins) > 1 or (origins and origins[0] not in self.allowed_origins)
                or b"cross-site" in sites):
            await self.reject(send, 403, "Origin is not allowed")
            return
        lengths = [v for k, v in headers if k.lower() == b"content-length"]
        if len(lengths) > 1 or (lengths and (len(lengths[0]) > 20 or not lengths[0].isdigit())):
            await self.reject(send, 400, "Invalid content length")
            return
        if lengths and int(lengths[0]) > maximum:
            await self.reject(send, 413, "Request is too large")
            return
        if self.active >= 4:
            await self.reject(send, 429, "Upload capacity exceeded")
            return
        self.active += 1
        try:
            with tempfile.TemporaryFile(mode="w+b") as buffered:
                total = 0
                deadline = time.monotonic() + 20
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        await self.reject(send, 408, "Upload deadline exceeded")
                        return
                    try:
                        message = await asyncio.wait_for(receive(), min(5, remaining))
                    except TimeoutError:
                        await self.reject(send, 408, "Upload deadline exceeded")
                        return
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        await self.reject(send, 400, "Invalid upload")
                        return
                    chunk = message.get("body", b"")
                    total += len(chunk)
                    if total > maximum:
                        await self.reject(send, 413, "Request is too large")
                        return
                    try:
                        buffered.write(chunk)
                    except OSError:
                        await self.reject(send, 503, "Temporary storage unavailable")
                        return
                    if not message.get("more_body", False):
                        break
                if lengths and total != int(lengths[0]):
                    await self.reject(send, 400, "Content length mismatch")
                    return
                buffered.seek(0)
                if check_json and total:
                    if not self.valid_file_paths(buffered.read()):
                        await self.reject(send, 403, "Invalid Gradio payload or file path")
                        return
                    buffered.seek(0)
                replayed = 0

                async def replay():
                    nonlocal replayed
                    if replayed >= total and replayed != -1:
                        replayed = -1
                        return {"type": "http.request", "body": b"", "more_body": False}
                    if replayed == -1:
                        return await receive()
                    chunk = buffered.read(65536)
                    replayed += len(chunk)
                    more = replayed < total
                    if not more:
                        replayed = -1
                    return {"type": "http.request", "body": chunk, "more_body": more}

                await self.app(scope, replay, send)
        finally:
            self.active -= 1
