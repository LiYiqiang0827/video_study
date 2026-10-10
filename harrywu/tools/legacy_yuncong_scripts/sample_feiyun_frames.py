import json
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SOURCE = Path(r"C:\Users\HarryWu_IOKI\Videos\飞云双突破\For GPT")
OUTPUT = Path(r"C:\Project Rocket\output\feiyun_double_breakout\visual_samples")
OUTPUT.mkdir(parents=True, exist_ok=True)


def duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def timestamp(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def main() -> None:
    font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 24)
    for video in sorted(SOURCE.glob("*.mkv")):
        target = OUTPUT / video.stem
        target.mkdir(exist_ok=True)
        total = duration(video)
        times = [30 + i * 180 for i in range(math.ceil((total - 30) / 180))]
        rows = []
        for second in times:
            frame = target / f"{second:05d}.jpg"
            if not frame.exists():
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(second), "-i", str(video), "-frames:v", "1", "-vf", "scale=960:540", "-q:v", "3", "-y", str(frame)],
                    check=True,
                )
            rows.append({"time": second, "image": str(frame)})
        for batch_index in range(math.ceil(len(rows) / 4)):
            page = Image.new("RGB", (1920, 1160), "#f7f7f7")
            draw = ImageDraw.Draw(page)
            for pos, item in enumerate(rows[batch_index * 4 : batch_index * 4 + 4]):
                x = (pos % 2) * 960
                y = (pos // 2) * 580
                with Image.open(item["image"]) as frame:
                    page.paste(frame, (x, y + 40))
                draw.text((x + 10, y + 5), timestamp(item["time"]), font=font, fill="black")
            page.save(target / f"sheet_{batch_index + 1:02d}.jpg", quality=88)
        (target / "index.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(video.name, len(rows), "frames", flush=True)


if __name__ == "__main__":
    main()
