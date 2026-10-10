"""Local, source-preserving media extraction and evidence workflows."""

SCHEMA_VERSION = "1.0"

# Resolve generic modules from the installed shared core. Project-specific
# workflows remain available on this package's original path. Without the
# distribution, the preserved legacy files continue to work as before.
from importlib.metadata import distribution, PackageNotFoundError
from pathlib import Path

try:
    _shared = Path(distribution("local-media-platform").locate_file("media_platform"))
except PackageNotFoundError:
    _shared = None
if _shared is not None and _shared.is_dir() and _shared.resolve() != Path(__file__).parent.resolve():
    __path__.insert(0, str(_shared))
    __version__ = distribution("local-media-platform").version
