"""Resolve module paths and keep runtime data outside the entire repository."""
from __future__ import annotations
import os
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = MODULE_ROOT.parent


def data_root() -> Path:
    return external_path(os.environ.get('YUNCONG_DATA_ROOT', str(Path.home() / 'YuncongVideoAnalysis')))


def external_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if path.is_relative_to(PROJECT_ROOT):
        raise ValueError(f'Runtime data must be outside the code project: {path}')
    return path
