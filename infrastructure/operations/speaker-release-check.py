#!/usr/bin/python3
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tarfile
import tempfile

BASE = Path("/var/lib/speaker-releases")
SOURCE = Path("/home/speaker-ci/agent/workspace/speaker-ci")
LIMIT = 2 * 1024**3


def open_directory(path):
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            following = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_file(directory, name, limit):
    fd = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=directory,
    )
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
        os.close(fd)
        raise ValueError("Unexpected release file")
    return os.fdopen(fd, "rb")


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run as root.")
    if len(sys.argv) != 2 or not re.fullmatch(r"[1-9][0-9]{0,8}", sys.argv[1]):
        raise SystemExit("Usage: speaker-release-check BUILD_NUMBER")

    number = sys.argv[1]
    reference = f"localhost/speaker-verification:ci-{number}"

    BASE.mkdir(mode=0o700, exist_ok=True)
    info = BASE.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & 0o077
    ):
        raise SystemExit("Unsafe destination directory.")

    directory = open_directory(SOURCE / f"release-{number}")
    try:
        metadata = {}
        for name in (
            "commit.txt", "image-id.txt",
            "image-reference.txt", "SHA256SUMS",
        ):
            with open_file(directory, name, 4096) as stream:
                metadata[name] = stream.read(4097).decode("ascii").strip()

        commit = metadata["commit.txt"]
        image_id = metadata["image-id.txt"].removeprefix("sha256:")
        checksum = metadata["SHA256SUMS"]

        if metadata["image-reference.txt"] != reference:
            raise ValueError("Unexpected image reference")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("Invalid commit")
        if not re.fullmatch(r"[0-9a-f]{64}", image_id):
            raise ValueError("Invalid image ID")
        if not re.fullmatch(r"[0-9a-f]{64}  image\.oci\.tar", checksum):
            raise ValueError("Invalid checksum receipt")

        stage = Path(tempfile.mkdtemp(prefix=f"ci-{number}-", dir=BASE))
        archive_path = stage / "image.oci.tar"

        with open_file(directory, "image.oci.tar", LIMIT) as incoming:
            with archive_path.open("xb") as outgoing:
                digest = hashlib.sha256()
                total = 0
                while chunk := incoming.read(1024 * 1024):
                    total += len(chunk)
                    if total > LIMIT:
                        raise ValueError("Archive size limit exceeded")
                    digest.update(chunk)
                    outgoing.write(chunk)

        archive_path.chmod(0o400)
        if digest.hexdigest() != checksum.split()[0]:
            raise ValueError("Archive checksum mismatch")
    finally:
        os.close(directory)

    with tarfile.open(archive_path, "r:") as archive:
        members = {}
        for member in archive:
            if member.name in members:
                raise ValueError("Duplicate archive entry")
            members[member.name] = member
            if len(members) > 512:
                raise ValueError("Too many archive entries")
            if member.isdir() and member.name in {"blobs", "blobs/sha256"}:
                continue
            if not member.isfile():
                raise ValueError("Unexpected archive entry type")
            if member.name not in {"oci-layout", "index.json"} and not re.fullmatch(
                r"blobs/sha256/[0-9a-f]{64}", member.name
            ):
                raise ValueError("Unexpected archive path")

        def read_json(name):
            member = members[name]
            if not member.isfile() or member.size > 1024 * 1024:
                raise ValueError("Unexpected JSON size")
            with archive.extractfile(member) as stream:
                return json.loads(stream.read())

        for name, member in members.items():
            if name.startswith("blobs/sha256/") and member.isfile():
                with archive.extractfile(member) as stream:
                    blob_hash = hashlib.file_digest(stream, "sha256").hexdigest()
                if blob_hash != name.rsplit("/", 1)[1]:
                    raise ValueError("Blob checksum mismatch")

        def blob(digest):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("Invalid OCI digest")
            return "blobs/sha256/" + digest[7:]

        if read_json("oci-layout").get("imageLayoutVersion") != "1.0.0":
            raise ValueError("Unexpected OCI layout")

        index = read_json("index.json")
        if len(index["manifests"]) != 1:
            raise ValueError("Expected one manifest")
        descriptor = index["manifests"][0]

        if descriptor.get("annotations", {}).get(
            "org.opencontainers.image.ref.name"
        ) != reference:
            raise ValueError("OCI reference mismatch")

        manifest = read_json(blob(descriptor["digest"]))
        if manifest["config"]["digest"] != "sha256:" + image_id:
            raise ValueError("Image ID mismatch")

        config = read_json(blob(manifest["config"]["digest"]))["config"]
        if config.get("User") != "1000:1000":
            raise ValueError("Unexpected image user")
        if config.get("Entrypoint") != ["python", "-m", "uvicorn"]:
            raise ValueError("Unexpected entrypoint")
        if config.get("Labels", {}).get(
            "org.opencontainers.image.revision"
        ) != commit:
            raise ValueError("Image commit mismatch")

        for layer in manifest["layers"]:
            if blob(layer["digest"]) not in members:
                raise ValueError("Missing image layer")

    receipt = {
        "build": number,
        "reference": reference,
        "commit": commit,
        "image_id": image_id,
        "manifest_digest": descriptor["digest"],
        "archive_sha256": digest.hexdigest(),
        "archive": str(archive_path),
    }
    receipt_path = stage / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    receipt_path.chmod(0o400)

    print("Release validation: PASSED")
    print("Root-owned release:", stage)
    print("Commit:", commit)
    print("Deployment unchanged.")


if __name__ == "__main__":
    main()
