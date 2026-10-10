from pathlib import Path
import hashlib
import json
import sys

import pandas as pd
import pyarrow.parquet as pq

OUT = Path(__file__).resolve().parent
REPO = Path('C:/Project Rocket/Codex/TradingAgent')
sys.path.insert(0, str(REPO))
from chart_tool.contracts import ChartView

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

overlap = read(OUT / 'daily_overlap.json')
registry = read(REPO / 'chart_tool/minute_sources.json')
assert registry['priority'] == ['csv_1min_original']
source = registry['sources'][0]
for receipt in overlap['stock_days']:
    day, sha = receipt['trade_date'], receipt['source_sha256']
    name = day + '.' + sha[:12] + '.parquet'
    production = OUT / 'production_cache/minutes/002025.SZ' / name
    table = pq.read_table(production)
    metadata = json.loads(table.schema.metadata[b'chart.minute.v1'])
    assert metadata['identity']['source'] == source
    assert metadata['identity']['cache_version'] == 2
    assert metadata['identity']['source_sha256'] == sha
    assert metadata['auction'] is None and metadata['auction_ohlc'] is None
    pd.testing.assert_frame_equal(table.to_pandas(), pd.read_parquet(OUT / 'cache/minutes/002025.SZ' / name))

expected_days = {
    '002025_20260918_100730': ['2026-09-14','2026-09-15','2026-09-16','2026-09-17','2026-09-18'],
    '航天电器_20260109_092233': ['2025-12-31','2026-01-05','2026-01-06','2026-01-07','2026-01-08'],
}
results = []
for stem, days in expected_days.items():
    paths = OUT / 'charts'
    run = read(paths / (stem + '.run.json'))
    geometry = read(paths / (stem + '.geometry.json'))
    assert run['status'] == 'ok' and run['pixel_check']['status'] == 'pass'
    views = {p: ChartView.from_dict(read(Path(path))) for p, path in run['views'].items()}
    fifteen = views['15m']
    assert list(dict.fromkeys(fifteen.bars.trade_date)) == days
    panel = geometry['panels']['15m']
    assert panel['body_width_px'] < panel['slot_px']
    assert len(panel['bars']) == len(fifteen.bars)
    intraday = views['intraday']
    current = intraday.bars.trade_date.iloc[0]
    assert current == days[-1]
    prev_close = intraday.metadata['prev_close'][current]
    assert prev_close > 0
    expected_avg = intraday.bars.amount.cumsum() / intraday.bars.vol.cumsum()
    avg_diff = float((intraday.bars.avg_price - expected_avg).abs().max())
    assert avg_diff < 1e-8
    assert len(geometry['panels']['intraday']['bars']) == len(intraday.bars)
    assert all(row['avg_price_y_px'] is not None for row in geometry['panels']['intraday']['bars'])
    for view in views.values():
        assert view.metadata['auctions'] == {}
        assert 'AUCTION_UNAVAILABLE' in view.metadata['warnings']
        assert view.bars.bar_end.max() <= run['requested_cutoff']
    png = Path(run['png'])
    assert hashlib.sha256(png.read_bytes()).hexdigest() == run['png_sha256']
    results.append(dict(png=str(png), sha256=run['png_sha256'], cutoff=run['requested_cutoff'],
        five_days=days, fifteen_rows=len(fifteen.bars), candle_width_px=panel['body_width_px'],
        candle_slot_px=panel['slot_px'], intraday_rows=len(intraday.bars),
        prev_close=prev_close, avg_price_max_error=avg_diff, pixel_check='pass',
        auction=None, warning='AUCTION_UNAVAILABLE', production_source=source['source_id'],
        manual_visual_review='PASS: primary inspected full PNGs and detail crops; five sessions separated, candles nonoverlapping, average and previous-close lines, complete percent ticks, Chinese header'))
summary = dict(status='PASS', production_enabled=True, source_id=source['source_id'],
    stock_days_validated=10, production_caches_match_sha_verified_fixtures=True,
    tests=dict(passed=411, failed=0, skipped=1, skip_reason='Windows symbolic-link privilege unavailable; deterministic reparse check covered separately',
        log=str(OUT/'full_tests2.log')), charts=results,
    known_limitations=['Full historical coverage has not been validated; each requested day remains gated.',
        'Pandas/NumPy timedelta deprecation warnings remain; no dependencies installed.',
        'Default 3840px output clipped percent suffixes; delivered charts use existing --width 5600 option.'])
(OUT / 'delivery_acceptance.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False, indent=2))
