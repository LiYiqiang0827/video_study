"""Resolve module paths and keep runtime data outside the entire repository."""
from __future__ import annotations
import os
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = MODULE_ROOT.parent
TEMP_ROOT = PROJECT_ROOT / "tmp"


def data_root() -> Path:
    return external_path(os.environ.get('YUNCONG_DATA_ROOT', str(TEMP_ROOT / 'runtime')))


def external_path(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    path = raw.resolve()
    temp_root = TEMP_ROOT.resolve()
    if temp_root != TEMP_ROOT:
        raise ValueError(f"Project temporary directory must not be a symlink: {TEMP_ROOT}")
    if raw.absolute().is_relative_to(TEMP_ROOT) and not path.is_relative_to(TEMP_ROOT):
        raise ValueError(f"Temporary paths must stay inside the project tmp directory: {raw}")
    if path.is_relative_to(PROJECT_ROOT) and not path.is_relative_to(TEMP_ROOT):
        raise ValueError(f'Runtime data must be outside the code project: {path}')
    return path
