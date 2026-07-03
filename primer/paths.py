"""Data-file locations.

`config.json`, `state.json`, `.env` and `primer.log` live in the repo root
(the package's parent directory), exactly where the old single-file version
kept them. Override with the `PRIMER_DATA_DIR` environment variable.
"""

import os
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("PRIMER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


DATA_DIR = data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "primer.log"
ENV_PATH = DATA_DIR / ".env"
