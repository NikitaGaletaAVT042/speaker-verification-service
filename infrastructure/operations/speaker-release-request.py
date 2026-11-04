#!/usr/bin/python3
import fcntl
import os
import re
import subprocess
import sys

ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "LANG": "C.UTF-8",
}


def main():
    if os.geteuid() != 0 or len(sys.argv) != 1:
        raise SystemExit("This command requires root and accepts no arguments.")

    request = sys.stdin.buffer.read(32)
    match = re.fullmatch(
        rb"(check|deploy) ([1-9][0-9]{0,8})\n?", request
    )
    if match is None:
        raise SystemExit("Expected: check BUILD_NUMBER or deploy BUILD_NUMBER")

    mode = match.group(1).decode("ascii")
    number = match.group(2).decode("ascii")

    fd = os.open(
        "/run/speaker-release-request.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        checked = subprocess.run(
            ["/usr/local/sbin/speaker-release-check", number],
            env=ENV,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if checked.returncode != 0:
            print("Release validation failed. Deployment unchanged.")
            raise SystemExit(1)

        pattern = (
            r"(?m)^Root-owned release: "
            r"/var/lib/speaker-releases/(ci-"
            + re.escape(number)
            + r"-[a-z0-9_]{8})$"
        )
        directories = re.findall(pattern, checked.stdout)
        if len(directories) != 1:
            raise SystemExit("Unexpected validator response.")

        print(checked.stdout, end="", flush=True)

        command = [
            "/usr/local/sbin/speaker-deploy",
            directories[0],
        ]
        if mode == "check":
            command.append("--dry-run")

        result = subprocess.run(
            command,
            env=ENV,
            timeout=900,
        )
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
