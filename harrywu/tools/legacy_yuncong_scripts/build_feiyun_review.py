import os
import subprocess
import sys
from pathlib import Path

source = Path(r"C:\Users\HarryWu_IOKI\Videos\飞云双突破\For GPT")
output = Path(r"C:\Project Rocket\output\feiyun_double_breakout\review_frames_v1")
os.environ["PYTHONPATH"] = r"C:\Users\HarryWu_IOKI\Documents\推し活" + os.pathsep + os.environ.get("PYTHONPATH", "")
for video in sorted(source.glob("*.mkv")):
    target = output / video.stem
    command = [sys.executable, "-u", "-m", "media_platform.frame_review", str(video), "--interval", "60", "--output", str(target)]
    if target.exists():
        command.append("--resume")
    subprocess.run(command, check=True)
