"""Local session protection. Windows uses user-bound DPAPI; POSIX uses owner-only files."""

import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from pathlib import Path

from .errors import AuthenticationExpired, SyncError


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise SyncError("State directory must not be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        if path.stat().st_uid != os.getuid():
            raise SyncError("State directory is owned by another user.")
        path.chmod(0o700)


def _dpapi(data: bytes, decrypt: bool) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    fn.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN, bound to the current Windows user.
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise SyncError("Windows could not protect/unprotect the session for this user.")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


class SessionStore:
    def __init__(self, directory: Path):
        private_dir(directory)
        self.path = directory / "session.bin"

    def save(self, cookies: list[dict]) -> None:
        # Do not persist identity-provider cookies, passwords or browser localStorage.
        cookies = [c for c in cookies if c.get("domain", "").lstrip(".") == "www.moodle.tum.de"]
        raw = json.dumps(cookies).encode()
        payload = b"DPAPI\0" + _dpapi(raw, False) if os.name == "nt" else b"POSIX\0" + raw
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix="session-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def load(self) -> list[dict]:
        try:
            if self.path.is_symlink():
                raise SyncError("Session file must not be a symbolic link.")
            if os.name != "nt" and self.path.stat().st_mode & 0o077:
                raise SyncError("Session permissions are too broad; chmod 600 the session file.")
            payload = self.path.read_bytes()
            prefix = b"DPAPI\0" if os.name == "nt" else b"POSIX\0"
            if not payload.startswith(prefix):
                raise AuthenticationExpired()
            raw = payload[len(prefix) :]
            if os.name == "nt":
                raw = _dpapi(raw, True)
            cookies = json.loads(raw)
            if not isinstance(cookies, list) or any(
                not isinstance(c, dict)
                or not all(isinstance(c.get(k), str) for k in ("name", "value", "domain"))
                or not isinstance(c.get("path", "/"), str)
                for c in cookies
            ):
                raise AuthenticationExpired()
            return cookies
        except (OSError, ValueError) as exc:
            raise AuthenticationExpired() from exc

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)
