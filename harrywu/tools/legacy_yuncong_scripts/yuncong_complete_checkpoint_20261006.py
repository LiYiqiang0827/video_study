import json
from pathlib import Path
from datetime import datetime, timezone

B = Path(r'D:\YuncongVideoAnalysis\ledger_2026_v0.1\text_priority_batch_20261005')
summary = json.loads((B / 'final_acceptance.json').read_text(encoding='utf-8'))
assert summary['completed_streams'] == 20 and not summary['remaining_streams']
now = datetime.now(timezone.utc).isoformat()
text = '\n'.join([
    '优先盘中直播20场 — 完成说明', '',
    '20/20场的转写、股票名称校正、切分和云盘上传均已完成。',
    f"原视频合计{summary['source_hours']:.2f}小时，研究文本保留源时段合计{summary['retained_source_window_hours']:.2f}小时。",
    '每场均保存研究文本、学员个股快评、校正版全文、原始全文、名称校正记录、时间切分索引和处理结果。',
    '无法确认的股票名称已标注并汇总；时间均为原视频偏移。',
    '本阶段未处理K线图、OCR或实际交易核验。其余14场未启动。', '',
    '从00_从这里开始_20场阅读索引.txt进入，或按日期打开场次文件夹。',
    '批次文件夹：https://drive.google.com/drive/folders/1g-iftms84Z0b5t4kHDSTOG9pzcJKP-UF',
    f'完成记录UTC：{now}', ''
])
(B / '当前进度_完成说明.txt').write_text(text, encoding='utf-8')
# Keep yesterday's pause intact as history while making the current state explicit.
checkpoint_path = B / '暂停断点.json'
checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
if checkpoint.get('status') == 'paused':
    checkpoint = {'previous_pause': checkpoint}
checkpoint.update(status='complete', completed_utc=now, completed_streams=20,
                  planned_streams=20, remaining=[], next_stream=None,
                  final_acceptance=str(B / 'final_acceptance.json'),
                  cloud_folder='https://drive.google.com/drive/folders/1g-iftms84Z0b5t4kHDSTOG9pzcJKP-UF')
checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'completed_streams': 20, 'progress_file': str(B / '当前进度_完成说明.txt')}))
