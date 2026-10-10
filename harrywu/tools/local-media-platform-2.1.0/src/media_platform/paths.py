"""Runtime configuration belongs to the user, never to a project/package."""
import os
from pathlib import Path

def platform_home():
    override = os.environ.get("MEDIA_PLATFORM_HOME")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
    return base / "media-platform"

def profile_path():
    override = os.environ.get("MEDIA_PLATFORM_PROFILE")
    return Path(override).expanduser() if override else platform_home() / "device_profile.json"

def model_cache():
    override = os.environ.get("MEDIA_PLATFORM_MODEL_CACHE")
    return Path(override).expanduser() if override else platform_home() / "models"
