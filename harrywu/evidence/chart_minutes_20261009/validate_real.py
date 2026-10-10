"""Frozen local copies of the ten requested stock-days; no source mutations."""
from pathlib import Path
import json
import sys

REPO = Path('C:/Project Rocket/Codex/TradingAgent')
sys.path.insert(0, str(REPO))
from chart_tool import minute_source as ms
from chart_tool.contracts import ChartRequest
from chart_tool.data import load_raw

OUT = Path(__file__).resolve().parent
V2 = Path('C:/Project Rocket/outputs/chart_tool_v2_20261009')
SNAP = Path('C:/Project Rocket/outputs/chart_tool_development_20261009/inputs')
registry = json.loads((V2 / 'inputs/minutes/registry_fixture.json').read_text(encoding='utf-8'))
registry['priority'] = ['csv_1min_original']
registry_path = OUT / 'validation_registry.json'
registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding='utf-8')
request = ChartRequest('002025.SZ', '2026-09-18T10:07:30+08:00', dict(
    kind='manual', confidence='high', evidence_refs=['user:2026-10-09:mechanical-minute-policy'],
    candidates=[], calibration_segment=None))
raw = load_raw(request, cache_dir=OUT / 'cache', snapshot_dir=SNAP)
fixtures = json.loads((V2 / 'X1/fixture_plan.json').read_text(encoding='utf-8'))['fixtures']
receipts = []
for fixture in fixtures:
    if fixture['purpose_id'] not in ['REG-1', 'CASE-1']:
        continue
    for day, expected_sha in zip(fixture['trade_dates'], fixture['sha256']):
        value = ms.load_minute_day(raw, day, registry_path=registry_path, cache_dir=OUT / 'cache')
        assert value is not None
        assert value.source_sha256 == expected_sha
        assert value.auction is None and len(value.one_min) == 240
        receipts.append(dict(purpose=fixture['purpose_id'], trade_date=day,
            source_id=value.source_id, source_sha256=value.source_sha256,
            rows=240, auction=None, first_bar=value.one_min.iloc[0].to_dict(), checks=value.checks))
        print(day + ' PASS', flush=True)
assert len(receipts) == 10
(OUT / 'daily_overlap.json').write_text(json.dumps(dict(status='PASS', policy='241_merge_0930_into_0931_or_240_shift_one_minute',
    source_fixture='SHA256-identical complete original CSV copies', stock_days=receipts), ensure_ascii=False, indent=2), encoding='utf-8')
