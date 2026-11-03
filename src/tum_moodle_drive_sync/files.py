import hashlib
import os
import re
import shutil
import unicodedata
import uuid
from email.message import Message
from pathlib import Path

from .errors import SyncError


def clean_name(name: str, limit=100) -> str:
    name = unicodedata.normalize("NFC", name)
    name = "".join("_" if unicodedata.category(c).startswith("C") else c for c in name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .") or "unnamed"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", name, re.I):
        name = "_" + name
    # Bound by UTF-8 bytes as well as characters for POSIX filesystem limits.
    suffix = Path(name).suffix[:16]
    stem = name[: -len(suffix)] if suffix else name
    stem = stem.encode()[: max(1, limit - len(suffix.encode()))].decode("utf-8", errors="ignore")
    return (stem or "file") + suffix


def disposition_name(header: str | None, fallback: str) -> str:
    if header:
        msg = Message()
        msg["content-disposition"] = header
        try:
            return clean_name(msg.get_filename() or fallback)
        except (LookupError, UnicodeError):
            pass
    return clean_name(fallback)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside(root: Path, path: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve()) and not path.is_symlink()


def publish(temp: Path, root: Path, resource, filename: str, digest: str) -> Path:
    folder = (
        root
        / f"{clean_name(resource.course.name, 55)} [{resource.course.id}]"
        / clean_name(resource.section, 45)
    )
    if not inside(root, folder):
        raise SyncError("Output path escapes the configured directory.")
    folder.mkdir(parents=True, exist_ok=True)
    if not inside(root, folder):
        raise SyncError("Output directory points outside the configured root.")
    name = Path(clean_name(filename))
    candidate = folder / f"{name.stem} [{resource.key[:10]}]{name.suffix}"
    # Existing files are never opened for writing. Both remote versions and user files survive.
    for attempt in range(100):
        if attempt:
            candidate = (
                folder
                / f"{name.stem} [{resource.key[:10]}-{digest[:10]}-{uuid.uuid4().hex[:6]}]{name.suffix}"
            )
        if os.name == "nt" and len(str(candidate)) > 240:
            raise SyncError(
                "Output path is too long for Windows; choose a shorter output directory."
            )
        if candidate.is_symlink():
            continue
        try:
            dest = candidate.open("xb")
        except FileExistsError:
            continue
        try:
            with dest, temp.open("rb") as src:
                shutil.copyfileobj(src, dest, 1024 * 1024)
                dest.flush()
                os.fsync(dest.fileno())
            return candidate
        except BaseException:
            candidate.unlink(missing_ok=True)  # Only the incomplete file created by this call.
            raise
    raise SyncError("Could not allocate a unique output filename.")
