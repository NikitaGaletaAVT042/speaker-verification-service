#!/usr/bin/python3
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import urllib.request

BASE = Path("/var/lib/speaker-releases")
K3S = "/usr/local/bin/k3s"
NAMESPACE = "speaker-verification"
DEPLOYMENT = "speaker-verification"
ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "KUBECONFIG": "/etc/rancher/k3s/k3s.yaml",
}


def root_owned(path, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != 0:
        raise ValueError("Unexpected file ownership or type")
    if info.st_mode & 0o022:
        raise ValueError("File is writable by another user")


def run(args, timeout=30):
    result = subprocess.run(
        [K3S, *args],
        env=ENV,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout


def kubectl(*args, timeout=30):
    return run(
        ["kubectl", "-n", NAMESPACE, *args],
        timeout=timeout,
    )


def deployment():
    return json.loads(kubectl(
        "get", "deployment", DEPLOYMENT, "-o", "json"
    ))


def api_image(document):
    containers = document["spec"]["template"]["spec"]["containers"]
    matches = [
        (index, item["image"])
        for index, item in enumerate(containers)
        if item["name"] == "api"
    ]
    if len(matches) != 1:
        raise ValueError("Expected exactly one api container")
    return matches[0]


def replace_image(document, expected, replacement):
    index, current = api_image(document)
    if current != expected:
        raise ValueError("Image changed concurrently")

    patch = [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": document["metadata"]["resourceVersion"],
        },
        {
            "op": "test",
            "path": f"/spec/template/spec/containers/{index}/name",
            "value": "api",
        },
        {
            "op": "test",
            "path": f"/spec/template/spec/containers/{index}/image",
            "value": expected,
        },
        {
            "op": "replace",
            "path": f"/spec/template/spec/containers/{index}/image",
            "value": replacement,
        },
    ]

    kubectl(
        "patch", "deployment", DEPLOYMENT,
        "--type=json", "-p", json.dumps(patch),
    )


def wait_ready():
    kubectl(
        "rollout", "status", f"deployment/{DEPLOYMENT}",
        "--timeout=240s", timeout=260,
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    )
    with opener.open(
        "http://127.0.0.1:30861/health/ready", timeout=5
    ) as response:
        payload = response.read(4097)
        if (
            response.status != 200
            or len(payload) > 4096
            or json.loads(payload).get("status") != "ready"
        ):
            raise ValueError("Application is not ready")


def images():
    found = {}
    for line in run(["ctr", "images", "list"]).splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2].startswith("sha256:"):
            found[fields[0]] = fields[2]
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("release")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise SystemExit("Run as root.")
    if not re.fullmatch(r"ci-[1-9][0-9]{0,8}-[a-z0-9_]{8}", args.release):
        raise SystemExit("Invalid release directory name.")

    os.umask(0o077)
    root_owned(BASE, directory=True)
    stage = BASE / args.release
    root_owned(stage, directory=True)

    receipt_path = stage / "receipt.json"
    archive = stage / "image.oci.tar"
    root_owned(receipt_path)
    root_owned(archive)

    if receipt_path.stat().st_size > 8192:
        raise ValueError("Receipt exceeds size limit")
    receipt = json.loads(receipt_path.read_text())

    number = receipt["build"]
    if not isinstance(number, str) or not re.fullmatch(
        r"[1-9][0-9]{0,8}", number
    ):
        raise ValueError("Invalid build number")
    if not args.release.startswith(f"ci-{number}-"):
        raise ValueError("Release number mismatch")

    reference = f"localhost/speaker-verification:ci-{number}"
    digest = receipt["manifest_digest"]
    if receipt["reference"] != reference:
        raise ValueError("Reference mismatch")
    if receipt["archive"] != str(archive):
        raise ValueError("Archive path mismatch")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Invalid manifest digest")
    if not 0 < archive.stat().st_size <= 2 * 1024**3:
        raise ValueError("Archive exceeds size limit")

    with archive.open("rb") as stream:
        actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual_hash != receipt["archive_sha256"]:
        raise ValueError("Root archive checksum mismatch")

    pinned = "localhost/speaker-verification@" + digest

    lock_fd = os.open(
        "/run/speaker-deploy.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        before = deployment()
        _, previous = api_image(before)

        print("Release:", reference, flush=True)
        print("Commit:", receipt["commit"], flush=True)
        print("Previous image:", previous, flush=True)
        print("New image:", pinned, flush=True)
        print("Root archive SHA-256: OK", flush=True)

        existing = images()
        for name in (reference, pinned):
            if name in existing and existing[name] != digest:
                raise ValueError("Existing image reference has another digest")

        if args.dry_run:
            print("Dry run: PASSED. Deployment unchanged.")
            return

        if previous == pinned:
            wait_ready()
            print("Already deployed and ready.")
            return

        backup = stage / "deployment-before.json"
        with backup.open("x") as output:
            json.dump(before, output, indent=2)
            output.write("\n")

        print("Importing verified archive...", flush=True)
        run(["ctr", "images", "import", str(archive)], timeout=180)

        existing = images()
        if existing.get(reference) != digest:
            raise ValueError("Imported manifest digest mismatch")
        if pinned not in existing:
            run(["ctr", "images", "tag", reference, pinned])

        if images().get(pinned) != digest:
            raise ValueError("Pinned image digest mismatch")

        print("Updating api image...", flush=True)
        try:
            replace_image(before, previous, pinned)
            wait_ready()
            if api_image(deployment())[1] != pinned:
                raise ValueError("Deployment image changed concurrently")
        except (
            subprocess.SubprocessError,
            OSError,
            ValueError,
        ) as error:
            print(
                "Deployment check failed:", type(error).__name__,
                flush=True,
            )
            current = deployment()
            current_image = api_image(current)[1]
            if current_image == pinned:
                print("Restoring previous image...", flush=True)
                replace_image(current, pinned, previous)
                wait_ready()
                print("Rollback: PASSED", flush=True)
            elif current_image != previous:
                raise RuntimeError(
                    "Concurrent image change; automatic rollback skipped"
                ) from error
            raise

        print("Deployment: PASSED")


if __name__ == "__main__":
    main()
