"""Write preflight for source namespaces and filesystem redirections."""
from __future__ import annotations
import os
from pathlib import Path
import stat
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
from time import perf_counter
from .contracts import ChartError


def protected_source_roots() -> list[Path]:
    project = Path(__file__).resolve().parents[1]
    candidates = [project / 'offlineDataManager/data',
                  Path('C:/Project Rocket/Trading Agent Database'),
                  Path('D:/ProjectRocketData/legacy_C_Project_Rocket/Trading Agent Database'),
                  Path('D:/ProjectRocketData/processed/TradingAgent')]
    for key in ('TRADING_AGENT_DATA_DIR', 'TRADING_AGENT_DERIVED_DATA_DIR'):
        if os.environ.get(key):
            candidates.append(Path(os.environ[key]))
    return [p.expanduser().resolve() for p in candidates]


def writable_path(path: str | Path, *, protected_roots=(), suffix: str | None = None) -> Path:
    """Reject before writing. This is a workflow guard, not an OS sandbox."""
    candidate = Path(path).expanduser().absolute()
    if suffix is not None and candidate.suffix.lower() != suffix:
        raise ChartError('OUTPUT_PATH', f'Output must use {suffix} suffix')
    for part in (candidate, *candidate.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or (getattr(info, 'st_file_attributes', 0) &
                                             getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
                raise ChartError('OUTPUT_PATH', 'Output links and reparse points are forbidden')
            if part == candidate and part.is_file() and info.st_nlink > 1:
                raise ChartError('OUTPUT_PATH', 'Output hard links are forbidden')
    resolved = candidate.resolve()
    roots = protected_source_roots() + [Path(p).expanduser().resolve() for p in protected_roots if p is not None]
    if any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise ChartError('OUTPUT_PATH', 'Output overlaps a read-only source namespace')
    return resolved


@contextmanager
def output_lock(target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = writable_path(target.with_suffix('.output.lock'))
    try:
        stream = lock.open('x',encoding='utf-8')
    except FileExistsError as exc:
        raise ChartError('OUTPUT_LOCKED','The same output bundle already has a writer') from exc
    try:
        with stream:
            json.dump({'pid':os.getpid(),'output':str(target)},stream)
        yield
    finally:
        lock.unlink(missing_ok=True)


def output_bundle(function):
    """One writer from before rendering through the final run receipt."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        roots=[kwargs.get('snapshot_dir'),kwargs.get('minute_snapshot_dir')]
        if kwargs.get('profile_path') is not None:
            roots.append(Path(kwargs['profile_path']).parent)
        target=writable_path(kwargs['output_path'],protected_roots=roots,suffix='.png')
        process_started=kwargs.pop('process_started',None)
        kwargs['output_path']=target
        with output_lock(target):
            report=function(*args,**kwargs)
            if process_started is not None:
                report['process_to_png_and_sidecars_seconds']=perf_counter()-process_started
            report['png_sha256']=hashlib.sha256(target.read_bytes()).hexdigest() if report['png'] else None
            from .io import write_json
            write_json(target.with_suffix('.run.json'),report)
            return report
    return wrapped
