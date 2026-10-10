import json
import os
import sys
import time
from pathlib import Path

import nvidia


SOURCE = Path(r"C:\Users\HarryWu_IOKI\Videos\飞云双突破\For GPT")
OUTPUT = Path(r"C:\Project Rocket\output\feiyun_double_breakout")
OUTPUT.mkdir(parents=True, exist_ok=True)

cuda_root = Path(list(nvidia.__path__)[0])
for name in ("cublas", "cudnn"):
    dll_dir = str(cuda_root / name / "bin")
    os.environ["PATH"] = dll_dir + os.pathsep + os.environ["PATH"]
    os.add_dll_directory(dll_dir)

from faster_whisper import WhisperModel  # noqa: E402


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    model = WhisperModel(
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        device="cuda",
        compute_type="int8_float16",
    )
    files = sorted(SOURCE.glob("*.mkv"))
    print(f"Loaded model; {len(files)} videos", flush=True)
    for path in files:
        stem = path.stem
        jsonl = OUTPUT / f"{stem}.jsonl"
        txt = OUTPUT / f"{stem}.txt"
        if jsonl.exists() and txt.exists():
            print(f"Skip complete: {path.name}", flush=True)
            continue
        started = time.monotonic()
        print(f"START {path.name}", flush=True)
        segments, info = model.transcribe(
            str(path),
            language="zh",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            temperature=0,
        )
        count = 0
        with jsonl.open("w", encoding="utf-8") as jf, txt.open("w", encoding="utf-8") as tf:
            for s in segments:
                record = {
                    "start": round(s.start, 3),
                    "end": round(s.end, 3),
                    "text": s.text.strip(),
                    "avg_logprob": round(s.avg_logprob, 4),
                    "no_speech_prob": round(s.no_speech_prob, 4),
                }
                jf.write(json.dumps(record, ensure_ascii=False) + "\n")
                tf.write(f"[{s.start:8.2f}–{s.end:8.2f}] {s.text.strip()}\n")
                count += 1
                if count % 100 == 0:
                    jf.flush()
                    tf.flush()
                    print(f"{path.name}: {count} segments, through {s.end:.0f}/{info.duration:.0f}s, elapsed {time.monotonic()-started:.0f}s", flush=True)
        print(f"DONE {path.name}: {count} segments, elapsed {time.monotonic()-started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
