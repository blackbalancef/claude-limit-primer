"""state.json model + atomic JSON writes."""

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from primer.paths import STATE_PATH


class State(BaseModel):
    """The single schedule (state.json). Everything optional: `{}` = fresh install."""

    model_config = ConfigDict(extra="allow")

    anchor_reset: str | None = None
    next_reset_epoch: float | None = None
    next_prime_epoch: float | None = None
    paused: bool | None = None
    last_prime_epoch: float | None = None
    last_prime_ok: bool | None = None
    last_prime_detail: str | None = None
    plan_start_epoch: float | None = None
    plan_end: str | None = None  # kept for backward-readable state
    plan_end_epoch: float | None = None
    retry_after_failure: bool | None = None
    retry_reason: str | None = None

    def pop_extra(self, key: str) -> None:
        """Drop a legacy extra key (e.g. the pre-rewrite 'plan_start') if present."""
        if self.__pydantic_extra__ is not None:
            self.__pydantic_extra__.pop(key, None)


def write_text_atomic(path: Path, text: str) -> None:
    """Write a file atomically to avoid truncated JSON on crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load_state() -> State:
    if STATE_PATH.exists():
        return State.model_validate(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    return State()


def save_state(state: State) -> None:
    write_json_atomic(STATE_PATH, state.model_dump(exclude_none=True))
