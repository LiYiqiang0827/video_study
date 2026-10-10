"""Read only completed ASR chunks, with compact source-video timestamps."""
import json
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

root = Path(r"D:\YuncongVideoAnalysis\ledger_2026_v0.1\text_pilot_20261004_0804")
start = float(sys.argv[1]) if len(sys.argv) > 1 else 0
end = float(sys.argv[2]) if len(sys.argv) > 2 else 99999
rows = []
for path in sorted((root / "asr/chunks").glob("[0-9][0-9][0-9][0-9][0-9].json")):
    chunk = json.loads(path.read_text(encoding="utf-8"))
    rows.extend(row for row in chunk["segments"] if start <= row["start"] < end)

def stamp(value):
    value = int(value)
    return f"{value//3600:02}:{value//60%60:02}:{value%60:02}"

group = []
for row in sorted(rows, key=lambda row: row["start"]):
    if group and (row["start"] - group[0]["start"] >= 30 or row["start"] - group[-1]["end"] > 8):
        print(f"[{stamp(group[0]['start'])}–{stamp(group[-1]['end'])}] " + "".join(r["text"] for r in group))
        group = []
    group.append(row)
if group:
    print(f"[{stamp(group[0]['start'])}–{stamp(group[-1]['end'])}] " + "".join(r["text"] for r in group))
print(json.dumps({"read_start":start,"read_end":end,"segments_read":len(rows)},ensure_ascii=False))
