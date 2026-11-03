import tomllib
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_path, user_data_path

from .errors import SyncError

SITE = "https://www.moodle.tum.de"


def default_config() -> Path:
    return user_config_path("tum-moodle-drive-sync", appauthor=False) / "config.toml"


def default_state() -> Path:
    return user_data_path("tum-moodle-drive-sync", appauthor=False)


@dataclass(frozen=True)
class Config:
    output_dir: Path
    state_dir: Path
    course_ids: tuple[int, ...] = ()
    request_interval: float = 1.0
    retries: int = 3
    timeout: float = 60.0
    revalidate_hours: float = 24.0
    max_file_mb: int = 512
    browser_channel: str | None = None


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
        allowed = set(Config.__dataclass_fields__)
        if set(data) - allowed:
            raise ValueError("Unknown configuration key")
        output = Path(data.pop("output_dir")).expanduser()
        state = Path(data.pop("state_dir", str(default_state()))).expanduser()
        if not output.is_absolute() or not state.is_absolute():
            raise ValueError("output_dir and state_dir must be absolute paths")
        output, state = output.resolve(), state.resolve()
        if output == state or output in state.parents or state in output.parents:
            raise ValueError("state_dir and output_dir must be separate, non-nested directories")
        ids = data.pop("course_ids", [])
        if not isinstance(ids, list) or any(type(i) is not int or i <= 1 for i in ids):
            raise ValueError("course_ids must be an array of course IDs greater than 1")
        cfg = Config(output, state, tuple(dict.fromkeys(ids)), **data)
        for key, low, high in [
            ("request_interval", 0.25, 60),
            ("timeout", 1, 600),
            ("revalidate_hours", 0.01, 8760),
            ("retries", 0, 5),
            ("max_file_mb", 1, 4096),
        ]:
            value = getattr(cfg, key)
            if type(value) not in (int, float) or not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
        if type(cfg.retries) is not int or type(cfg.max_file_mb) is not int:
            raise ValueError("retries and max_file_mb must be integers")
        if cfg.browser_channel not in (None, "chrome", "msedge"):
            raise ValueError("browser_channel must be chrome or msedge, or omitted")
        return cfg
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SyncError(
            f"Cannot load config {path}: check TOML keys, paths and value types."
        ) from exc
