"""Keep runtime data outside the version-controlled tool directory."""
from __future__ import annotations
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_root() -> Path:
    return external_path(os.environ.get('YUNCONG_DATA_ROOT', '/Users/nickzhang/YuncongVideoAnalysis'))


def external_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if path.is_relative_to(PROJECT_ROOT):
        raise ValueError(f'Runtime data must be outside the code project: {path}')
    return path
