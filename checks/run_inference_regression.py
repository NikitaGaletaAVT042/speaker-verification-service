import hashlib
from pathlib import Path
import runpy
import secrets

verifier = Path("/tmp/test-metrics-token.sha256")
verifier.write_text(
    hashlib.sha256(secrets.token_bytes(32)).hexdigest()
)

runpy.run_path("/ci-checks/check_metrics.py", run_name="__main__")
