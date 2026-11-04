import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повторный ключ в конфигурации авторизации")
        result[key] = value
    return result


class PasswordStore:
    def __init__(self, path: Path):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

        with os.fdopen(os.open(path, flags), "rb") as file:
            info = os.fstat(file.fileno())

            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Ожидался обычный файл авторизации")

            if stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Файл авторизации должен иметь права 600")

            if not 0 < info.st_size <= 64 * 1024:
                raise ValueError("Некорректный размер файла авторизации")

            raw = file.read(64 * 1024 + 1)

        if len(raw) > 64 * 1024:
            raise ValueError("Превышен размер конфигурации авторизации")

        document = json.loads(raw, object_pairs_hook=_unique_keys)

        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "users"}
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
        ):
            raise ValueError("Неизвестный формат авторизации")

        users = document["users"]

        if not isinstance(users, dict) or not 1 <= len(users) <= 100:
            raise ValueError("Некорректное число пользователей")

        self._users = {}

        for username, record in users.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
                raise ValueError("Некорректное имя пользователя")

            if not isinstance(record, dict) or set(record) != {
                "algorithm", "iterations", "salt_hex", "password_hash_hex"
            }:
                raise ValueError("Некорректная запись пользователя")

            if (
                record["algorithm"] != "pbkdf2_sha256"
                or type(record["iterations"]) is not int
                or record["iterations"] != 600_000
            ):
                raise ValueError("Неподдерживаемые параметры хеширования")

            for key in ("salt_hex", "password_hash_hex"):
                value = record[key]
                if (
                    not isinstance(value, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value)
                ):
                    raise ValueError("Некорректный формат соли или хеша")

            self._users[username] = (
                bytes.fromhex(record["salt_hex"]),
                bytes.fromhex(record["password_hash_hex"]),
            )

        # Неизвестный пользователь тоже проходит дорогое вычисление хеша.
        self._dummy = (os.urandom(32), os.urandom(32))

    def verify(self, username: str, password: str) -> bool:
        if not isinstance(username, str) or not isinstance(password, str):
            return False

        if not 1 <= len(username) <= 64 or not 8 <= len(password) <= 256:
            return False

        try:
            encoded = password.encode("utf-8")
        except UnicodeEncodeError:
            return False

        record = self._users.get(username)
        salt, expected = record if record is not None else self._dummy

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            encoded,
            salt,
            600_000,
        )

        matches = hmac.compare_digest(actual, expected)
        return record is not None and matches
