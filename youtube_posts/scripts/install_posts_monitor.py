#!/usr/bin/env python3
"""Install the half-hour Posts monitor as a silent Hermes script-only Feishu cron job."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[2]
MONITOR = PROJECT / "youtube_posts/scripts/monitor_new_posts.py"
JOB_NAME = "yuncong-youtube-posts-monitor"
SCHEDULE = "*/30 9-23 * * *"
DIRECTORY = Path.home() / ".hermes/channel_directory.json"
WRAPPER = Path.home() / ".hermes/scripts/yuncong_posts_monitor.py"


def main():
    hermes = shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")
    if not Path(hermes).is_file():
        raise SystemExit("Hermes CLI was not found")
    directory = json.loads(DIRECTORY.read_text(encoding="utf-8"))
    targets = [item for item in directory.get("platforms", {}).get("feishu", [])
               if item.get("type") == "dm" and item.get("id")]
    if len(targets) != 1:
        raise SystemExit("Expected exactly one saved Feishu direct-message target; no cron job was created")
    listed = subprocess.run([hermes, "cron", "list", "--all"], capture_output=True, text=True)
    if listed.returncode:
        raise SystemExit("Could not inspect existing Hermes cron jobs")
    if JOB_NAME in listed.stdout:
        print("Hermes Posts monitor cron already exists; no duplicate was created.")
        return 0

    WRAPPER.parent.mkdir(parents=True, exist_ok=True)
    wrapper_text = (
        "#!/usr/bin/env python3\n"
        "import subprocess, sys\n"
        f"result = subprocess.run([sys.executable, {str(MONITOR)!r}], capture_output=True, text=True)\n"
        "if result.returncode:\n"
        "    sys.stderr.write(result.stderr or 'Posts monitor failed\\n')\n"
        "    raise SystemExit(result.returncode)\n"
        "sys.stdout.write(result.stdout)\n"
    )
    temp = WRAPPER.with_suffix(".tmp")
    temp.write_text(wrapper_text, encoding="utf-8")
    temp.chmod(0o700)
    temp.replace(WRAPPER)

    target = "feishu:" + str(targets[0]["id"])
    command = [hermes, "cron", "create", SCHEDULE, "--name", JOB_NAME,
               "--deliver", target, "--failure-deliver", "local",
               "--script", WRAPPER.name, "--no-agent"]
    created = subprocess.run(command, capture_output=True, text=True)
    if created.returncode:
        WRAPPER.unlink(missing_ok=True)
        # CLI output can echo the DM identifier; redact it before surfacing diagnostics.
        diagnostic = (created.stderr or created.stdout).replace(target, "feishu:<redacted>").strip()
        raise SystemExit("Hermes rejected cron creation: " + (diagnostic or "no diagnostic returned"))
    print("Created Hermes script-only monitor: 09:00–23:30 Asia/Shanghai, every 30 minutes; Feishu recipient redacted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
