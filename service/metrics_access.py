"""A service token grants only GET /metrics; all other access keeps Basic auth."""

import hashlib
import hmac
from pathlib import Path
import re


class MetricsAccess:
    def __init__(self, authenticated_app, metrics_app, digest_path):
        path = Path(digest_path)
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 128:
            raise ValueError("Invalid metrics-token verifier file")
        digest = path.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid metrics-token verifier")
        self.digest = digest
        self.authenticated_app = authenticated_app
        self.metrics_app = metrics_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"] == "/metrics":
            values = [value for key, value in scope["headers"] if key.lower() == b"authorization"]
            if len(values) == 1 and len(values[0]) <= 256:
                parts = values[0].split(b" ", 1)
                if len(parts) == 2 and parts[0].lower() == b"bearer":
                    candidate = hashlib.sha256(parts[1]).hexdigest()
                    if hmac.compare_digest(candidate, self.digest):
                        await self.metrics_app(scope, receive, send)
                        return
        await self.authenticated_app(scope, receive, send)
