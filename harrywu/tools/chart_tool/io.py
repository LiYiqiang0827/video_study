"""Serialization of processed views only; raw cache never enters analyst inputs."""
from __future__ import annotations

import json
from pathlib import Path
import os
import uuid
from .contracts import ChartView
from .paths import writable_path


def view_to_dict(view: ChartView) -> dict:
    return view.to_dict()


def view_from_dict(payload: dict) -> ChartView:
    return ChartView.from_dict(payload)


def write_json(path: str | Path, value: dict | list) -> Path:
    """Atomic replacement for root-owned non-immutable reports, never case records."""
    path = writable_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path
