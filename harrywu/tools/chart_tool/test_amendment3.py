"""Amendment 3 synthetic schema checks and copied, read-only legacy regressions."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd
import pytest

from chart_tool import cases
from chart_tool.contracts import (ChartError, ChartView, INTRADAY_COLUMNS,
                                  MINUTE_COLUMNS, MinuteDay, RenderOptions, validate_bars)
from chart_tool.test_cases import make_record, freeze, result
from chart_tool.test_multiperiod import synthetic as raw_bundle

V2 = Path('C:/Project Rocket/outputs/chart_tool_v2_20261009')
F2 = Path('C:/Project Rocket/outputs/chart_tool_development_20261009/F2')


def fail(code, operation):
    with pytest.raises(ChartError) as error:
        operation()
    assert error.value.code == code


def minute_frame(day='2026-09-30'):
    rows = []
    for clock in ('09:30', '13:00'):
        start = datetime.fromisoformat(day + 'T' + clock + ':00+08:00')
        for i in range(120):
            rows.append(dict(trade_date=day, open=10., high=11., low=9., close=10.,
                             vol=0., amount=0., bar_start=(start + timedelta(minutes=i)).isoformat(),
                             bar_end=(start + timedelta(minutes=i + 1)).isoformat(), is_partial=False))
    return pd.DataFrame(rows, columns=MINUTE_COLUMNS)


def a3_record(root):
    record = make_record(root)
    transcript = root / 'inputs/transcript.txt'
    transcript.write_text('SYNTHETIC_ENGINEERING_ONLY [s00001] hold [s00010]', encoding='utf-8')
    record['input_refs'][1]['sha256'] = hashlib.sha256(transcript.read_bytes()).hexdigest()
    frame = root / 'inputs/frame.png'
    frame.write_bytes(b'SYNTHETIC_ENGINEERING_ONLY')
    record['input_refs'].append(dict(kind='frame', path='inputs/frame.png',
                                     sha256=hashlib.sha256(frame.read_bytes()).hexdigest(),
                                     visible_through=record['request']['cutoff']))
    record.update(mention_type='retrospective', narrated_actions=[dict(
        action='hold', tense='past', bar_ref=dict(trade_date='2026-09-30', period='daily', basis='explicit', note=None),
        price_text=None, stated_reason=None, stated_expectation=None, stated_outcome=None,
        evidence_refs=['inputs/transcript.txt'], segment_ids=['s00001'], verification='transcript_unverified')],
        frame_facts=[dict(field='chart_period', value_text='SYNTHETIC daily', frame_ref='inputs/frame.png',
                          region='top', read_by='ai_visual', verified=False)],
        frame_window=dict(period='daily', first_visible_trade_date='2026-09-01',
                          last_visible_trade_date='2026-09-30', bar_count_estimate=20, tolerance_bars=2,
                          basis='visual_estimate', evidence_ref='inputs/frame.png',
                          frame_chart_region_px=[0, 0, 100, 100], note=None))
    return record


def a3_result(root):
    frozen = freeze(root, make_record(root))
    value = result(root, frozen)
    value['events'][0]['kind'] = 'first_available'
    value.update(price_anchor=dict(anchor_trade_date='2026-09-30', anchor_value=1., source='frozen_restore_view'),
        mention_day=dict(trade_date='2026-09-30', policy='postclose_excluded', reference_session_date='2026-10-01',
                         post_mention_high=None, post_mention_low=None, minute_source_id=None, note=None),
        path_snapshot=dict(method='fixed_horizon_v1', price_basis='restore_anchor_qfq',
            reference=dict(kind='next_session_open', trade_date='2026-10-01', at='2026-10-01T09:25:00+08:00', price=10., raw_price=10.),
            horizons=[dict(k=k, trade_date=None, close=None, close_return=None, mfe=None, mfe_trade_date=None,
                           mae=None, mae_trade_date=None, complete=False) for k in (1, 3, 5, 10, 20)], notes=[]))
    return value


def complete_horizon(value, index, day):
    value['path_snapshot']['horizons'][index].update(trade_date=day, close=10., close_return=0.,
                                                   mfe=.1, mfe_trade_date=day, mae=-.1, mae_trade_date=day, complete=True)


def view(root):
    make_record(root)
    return ChartView.from_dict(json.loads((root / 'inputs/view.json').read_text(encoding='utf-8')))


def test_metadata_both_generations_and_identity_unchanged(tmp_path):
    v = view(tmp_path)
    identity = deepcopy(v.identity())
    assert ChartView.from_dict(v.to_dict()).identity() == identity
    v.metadata.update(auctions={}, prev_close={})
    v.validate()
    v.metadata['auctions']['2026-09-30'] = dict(price=10., vol=0., amount=0., available_at='2026-09-30T09:25:00+08:00', source_id='synthetic')
    v.metadata['prev_close']['2026-09-30'] = 10.
    v.validate()


@pytest.mark.parametrize('change', [{'price_scale': []}, {'price_scale': {}}, {'auto_width': []},
                                  {'min_slot_px': []}, {'max_width_px': []}, {'annotations': []}])
def test_render_options_invalid_c0_shapes(change):
    fail('INVALID_REQUEST', lambda: replace(RenderOptions(), **change).validate())


@pytest.mark.parametrize('kind,code', [('one_key','INVALID_SCHEMA'), ('extra','INVALID_SCHEMA'),
    ('auction_future','CUTOFF_LEAK'), ('auction_wrong_clock','CUTOFF_LEAK'), ('auction_date','INVALID_SCHEMA'),
    ('auction_price','INVALID_VALUE'), ('auction_vol','INVALID_VALUE'), ('auction_amount','INVALID_VALUE'),
    ('auction_source','INVALID_SCHEMA'), ('auction_extra','INVALID_SCHEMA'), ('prev_future','CUTOFF_LEAK'),
    ('prev_price','INVALID_VALUE'), ('dict_type','INVALID_SCHEMA')])
def test_metadata_rejections(tmp_path, kind, code):
    v = view(tmp_path)
    v.metadata.update(auctions={'2026-09-30': dict(price=10., vol=0., amount=0., available_at='2026-09-30T09:25:00+08:00', source_id='synthetic')}, prev_close={})
    auction = v.metadata['auctions']['2026-09-30']
    if kind == 'one_key': del v.metadata['prev_close']
    elif kind == 'extra': v.metadata['unexpected'] = 1
    elif kind == 'auction_future':
        v.metadata['requested_cutoff'] = '2026-09-30T09:24:59+08:00'
        v.metadata['effective_cutoff'] = '2026-09-29T15:00:00+08:00'
    elif kind == 'auction_wrong_clock': auction['available_at'] = '2026-09-30T09:24:00+08:00'
    elif kind == 'auction_date': v.metadata['auctions'] = {'bad': auction}
    elif kind.startswith('auction_'): auction[kind.removeprefix('auction_').replace('source', 'source_id')] = '' if kind == 'auction_source' else -1
    elif kind == 'prev_future': v.metadata['prev_close']['2026-10-01'] = 10.
    elif kind == 'prev_price': v.metadata['prev_close']['2026-09-30'] = 0.
    else: v.metadata['prev_close'] = []
    fail(code, v.validate)


def test_auction_requested_cutoff_not_effective(tmp_path):
    v = view(tmp_path)
    v.bars = v.bars.iloc[0:0].copy()
    v.metadata['requested_cutoff'] = '2026-09-30T09:25:00+08:00'
    v.metadata['effective_cutoff'] = '2026-09-29T15:00:00+08:00'
    v.metadata['factor_lineage'] = None
    v.metadata.update(auctions={'2026-09-30': dict(price=10., vol=0., amount=0., available_at='2026-09-30T09:25:00+08:00', source_id='synthetic')}, prev_close={})
    v.validate()
    v.metadata['requested_cutoff'] = '2026-09-30T09:24:59+08:00'
    fail('CUTOFF_LEAK', v.validate)


@pytest.mark.parametrize('value,code', [(0., 'INVALID_VALUE'), (-1., 'INVALID_VALUE'), (float('inf'), 'INVALID_VALUE'), (float('nan'), 'INVALID_VALUE'), (True, 'INVALID_SCHEMA')])
def test_intraday_average(tmp_path, value, code):
    frame = minute_frame()
    validate_bars(frame, 'intraday')
    frame['avg_price'] = 10.
    assert tuple(frame.columns) == INTRADAY_COLUMNS
    validate_bars(frame, 'intraday')
    fail('INVALID_SCHEMA', lambda: validate_bars(frame, '1m'))
    fail('INVALID_SCHEMA', lambda: validate_bars(frame, '15m'))
    fail('INVALID_SCHEMA', lambda: validate_bars(frame, 'daily'))
    frame['avg_price'] = value
    fail(code, lambda: validate_bars(frame, 'intraday'))


def test_restore_anchor(tmp_path):
    v = view(tmp_path)
    v.metadata['factor_lineage']['selection'] = 'restore_anchor'
    fail('CUTOFF_LEAK', v.validate)
    v.metadata['stage'] = 'reveal'
    v.validate()
    v.metadata['factor_lineage']['anchor_trade_date'] = '2026-10-01'
    fail('CUTOFF_LEAK', v.validate)


def test_restore_anchor_does_not_use_last_closed_rule(tmp_path):
    v = view(tmp_path)
    v.bars = v.bars.iloc[0:0].copy()
    v.metadata.update(stage='reveal', cutoff_policy='minute_end_v1', requested_cutoff='2026-09-30T10:00:00+08:00', effective_cutoff='2026-09-30T10:00:00+08:00')
    v.metadata['factor_lineage'].update(selection='restore_anchor', anchor_trade_date='2026-09-30', anchor_available_at=None)
    v.validate()


@pytest.mark.parametrize('kind', ['date','price','vol','amount','source','extra'])
def test_raw_auctions(kind):
    raw = raw_bundle()
    raw.one_min = minute_frame()
    raw.metadata['series']['one_min'] = deepcopy(raw.metadata['series']['daily'])
    raw.auctions = {'2026-09-30': dict(price=10., vol=0., amount=0., source_id='synthetic')}
    raw.validate()
    if kind == 'date': raw.auctions = {'2026-09-29': next(iter(raw.auctions.values()))}
    else: next(iter(raw.auctions.values()))[kind.replace('source','source_id')] = -1
    with pytest.raises(ChartError): raw.validate()


@pytest.mark.parametrize('kind', ['short','wrong_date','partial','bad_grid','auction','source','sha','checks','extra_column'])
def test_minute_day(kind):
    day = MinuteDay('002025.SZ', '2026-09-30', minute_frame(), dict(price=10., vol=0., amount=0.), 'synthetic', 'synthetic', '0' * 64, {})
    original = day.one_min.copy(deep=True)
    day.validate()
    pd.testing.assert_frame_equal(day.one_min, original)
    MinuteDay(day.ts_code, day.trade_date, day.one_min, None, day.source_id, day.source_family, day.source_sha256, {}).validate()
    if kind == 'short': day = replace(day, one_min=day.one_min.iloc[:-1].copy())
    elif kind == 'wrong_date': day = replace(day, trade_date='2026-09-29')
    elif kind == 'partial': day.one_min.loc[239, 'is_partial'] = True
    elif kind == 'bad_grid':
        day.one_min.loc[0, 'bar_start'] = '2026-09-30T09:29:00+08:00'
        day.one_min.loc[0, 'bar_end'] = '2026-09-30T09:30:00+08:00'
    elif kind == 'auction': day = replace(day, auction=dict(price=10., vol=-1., amount=0.))
    elif kind == 'source': day = replace(day, source_id='')
    elif kind == 'sha': day = replace(day, source_sha256='x')
    elif kind == 'checks': day = replace(day, checks=[])
    else: day.one_min['avg_price'] = 10.
    with pytest.raises(ChartError): day.validate()


def test_new_restore_freeze_and_allowed_alternatives(tmp_path):
    record = a3_record(tmp_path)
    frozen = freeze(tmp_path, record)
    assert cases.load_restore_input(tmp_path, frozen['case_id'], 1) == record
    record['narrated_actions'][0]['bar_ref'].update(basis='inferred', note='SYNTHETIC candidate')
    cases._validate_restore(tmp_path, record)
    record['narrated_actions'][0]['bar_ref'].update(basis='unknown', trade_date=None)
    cases._validate_restore(tmp_path, record)
    record['narrated_actions'][0].update(tense='plan', evidence_refs=['inputs/frame.png'], segment_ids=[])
    record['narrated_actions'][0]['bar_ref'].update(basis='explicit', note=None)
    record['frame_facts'][0].update(read_by='human', verified=True)
    cases._validate_restore(tmp_path, record)
    record['frame_window'] = None
    cases._validate_restore(tmp_path, record)


@pytest.mark.parametrize('kind,code', [('one_key','INVALID_SCHEMA'), ('mention','INVALID_SCHEMA'), ('action','INVALID_SCHEMA'),
    ('tense','INVALID_SCHEMA'), ('verification','INVALID_SCHEMA'), ('period','INVALID_SCHEMA'), ('basis','INVALID_SCHEMA'),
    ('future','CUTOFF_LEAK'), ('inferred_note','INVALID_SCHEMA'), ('unknown_note','INVALID_SCHEMA'),
    ('null_explicit','INVALID_SCHEMA'), ('unknown_date','INVALID_SCHEMA'), ('plan_date','INVALID_SCHEMA'),
    ('long_text','INVALID_SCHEMA'), ('nonstring','INVALID_SCHEMA'), ('no_refs','INVALID_SCHEMA'),
    ('view_ref','CASE_EVIDENCE'), ('missing_segment','CASE_EVIDENCE'), ('segment_prefix','CASE_EVIDENCE'),
    ('no_segments','CASE_EVIDENCE'), ('frame_segment','CASE_EVIDENCE'), ('fact_field','INVALID_SCHEMA'),
    ('fact_verified','INVALID_SCHEMA'), ('fact_boolean','INVALID_SCHEMA'), ('fact_read_by','INVALID_SCHEMA'),
    ('fact_ref','CASE_EVIDENCE'), ('fact_long','INVALID_SCHEMA'), ('window_future','CUTOFF_LEAK'),
    ('window_order','INVALID_SCHEMA'), ('window_period','INVALID_SCHEMA'), ('window_basis','INVALID_SCHEMA'),
    ('window_count','INVALID_SCHEMA'), ('window_tolerance','INVALID_SCHEMA'), ('window_ref','CASE_EVIDENCE'),
    ('window_region','INVALID_SCHEMA'), ('extra_action','INVALID_SCHEMA')])
def test_new_restore_rejections(tmp_path, kind, code):
    r = a3_record(tmp_path)
    a, b, f, w = r['narrated_actions'][0], r['narrated_actions'][0]['bar_ref'], r['frame_facts'][0], r['frame_window']
    if kind == 'one_key': del r['frame_window']
    elif kind == 'mention': r['mention_type'] = 'bad'
    elif kind in {'action','tense','verification'}: a[kind] = 'bad'
    elif kind in {'period','basis'}: b[kind] = 'bad'
    elif kind == 'future': b['trade_date'] = '2026-10-01'
    elif kind == 'inferred_note': b.update(basis='inferred', note='')
    elif kind == 'unknown_note': b.update(basis='unknown', trade_date=None)
    elif kind == 'null_explicit': b['trade_date'] = None
    elif kind == 'unknown_date': b.update(basis='unknown', note='uncertain')
    elif kind == 'plan_date': a['tense'] = 'plan'
    elif kind == 'long_text': a['stated_reason'] = 'x' * 201
    elif kind == 'nonstring': a['price_text'] = 10
    elif kind == 'no_refs': a['evidence_refs'] = []
    elif kind == 'view_ref': a['evidence_refs'] = ['inputs/view.json']
    elif kind == 'missing_segment': a['segment_ids'] = ['s99999']
    elif kind == 'segment_prefix': a['segment_ids'] = ['s0001']
    elif kind == 'no_segments': a['segment_ids'] = []
    elif kind == 'frame_segment': a['evidence_refs'] = ['inputs/frame.png']
    elif kind == 'fact_field': f['field'] = 'bad'
    elif kind == 'fact_verified': f['verified'] = True
    elif kind == 'fact_boolean': f['verified'] = 1
    elif kind == 'fact_read_by': f['read_by'] = 'bad'
    elif kind == 'fact_ref': f['frame_ref'] = 'inputs/transcript.txt'
    elif kind == 'fact_long': f['value_text'] = 'x' * 201
    elif kind == 'window_future': w['last_visible_trade_date'] = '2026-10-01'
    elif kind == 'window_order': w['first_visible_trade_date'] = '2026-10-01'
    elif kind == 'window_period': w['period'] = '1m'
    elif kind == 'window_basis': w['basis'] = 'bad'
    elif kind == 'window_count': w['bar_count_estimate'] = True
    elif kind == 'window_tolerance': w['tolerance_bars'] = -1
    elif kind == 'window_ref': w['evidence_ref'] = 'inputs/transcript.txt'
    elif kind == 'window_region': w['frame_chart_region_px'] = [100, 0, 0, 100]
    else: a['unexpected'] = True
    fail(code, lambda: cases.save_draft(tmp_path, r))


def test_new_result_completed_and_incomplete(tmp_path):
    r = a3_result(tmp_path)
    cases.append_result(tmp_path, r)
    assert cases.load_results(tmp_path, r['case_id'], 1) == [r]
    r['result_sequence'] = 2
    r['observed_through'] = '2026-11-30T15:00:00+08:00'
    for i, day in enumerate(('2026-10-01', '2026-10-03', '2026-10-05', '2026-10-10', '2026-10-20')):
        complete_horizon(r, i, day)
    r.update(complete=True, coverage_warning=None)
    # All five roles can share a timestamp in their specified order.
    r['events'] = [{**r['events'][0], 'kind': k} for k in cases._EVENT_KINDS]
    cases.append_result(tmp_path, r)


@pytest.mark.parametrize('kind,code', [('one_key','INVALID_SCHEMA'), ('anchor_source','INVALID_SCHEMA'),
    ('anchor_value','INVALID_SCHEMA'), ('anchor_date','INVALID_SCHEMA'), ('policy','INVALID_SCHEMA'),
    ('mention_price','INVALID_SCHEMA'), ('mention_note','INVALID_SCHEMA'), ('method','INVALID_SCHEMA'),
    ('basis','INVALID_SCHEMA'), ('ref_kind','INVALID_SCHEMA'), ('ref_price','INVALID_SCHEMA'),
    ('ref_date','INVALID_SCHEMA'), ('notes','INVALID_SCHEMA'), ('k_count','INVALID_SCHEMA'),
    ('k_order','INVALID_SCHEMA'), ('k_bool','INVALID_SCHEMA'), ('partial_null','INVALID_SCHEMA'),
    ('complete_null','INVALID_SCHEMA'), ('dates','INVALID_SCHEMA'), ('bounds','INVALID_SCHEMA'),
    ('close','INVALID_SCHEMA'), ('extreme_date','INVALID_SCHEMA'), ('complete_mismatch','INVALID_SCHEMA'),
    ('unknown_event','INVALID_SCHEMA'), ('duplicate_event','INVALID_SCHEMA'), ('tie_order','INVALID_SCHEMA'),
    ('event_at_mention','CUTOFF_LEAK'), ('event_future','CUTOFF_LEAK'), ('event_decreasing','CUTOFF_LEAK')])
def test_new_result_rejections(tmp_path, kind, code):
    r = a3_result(tmp_path)
    path = r['path_snapshot']
    if kind == 'one_key': del r['price_anchor']
    elif kind == 'anchor_source': r['price_anchor']['source'] = 'bad'
    elif kind == 'anchor_value': r['price_anchor']['anchor_value'] = 0
    elif kind == 'anchor_date': r['price_anchor']['anchor_trade_date'] = 'bad'
    elif kind == 'policy': r['mention_day']['policy'] = 'bad'
    elif kind == 'mention_price': r['mention_day']['post_mention_low'] = 0
    elif kind == 'mention_note': r['mention_day']['note'] = 1
    elif kind in {'method','basis'}: path['method' if kind == 'method' else 'price_basis'] = 'bad'
    elif kind == 'ref_kind': path['reference']['kind'] = 'bad'
    elif kind == 'ref_price': path['reference']['raw_price'] = True
    elif kind == 'ref_date': path['reference']['trade_date'] = '2026-10-02'
    elif kind == 'notes': path['notes'] = [1]
    elif kind == 'k_count': path['horizons'].pop()
    elif kind == 'k_order': path['horizons'][0]['k'] = 3
    elif kind == 'k_bool': path['horizons'][0]['k'] = True
    elif kind == 'partial_null': path['horizons'][0]['close'] = 10.
    elif kind == 'complete_null': path['horizons'][4]['complete'] = True
    elif kind == 'dates':
        complete_horizon(r, 0, '2026-10-01')
        complete_horizon(r, 1, '2026-10-01')
    elif kind == 'bounds':
        complete_horizon(r, 0, '2026-10-01')
        path['horizons'][0]['close_return'] = .2
    elif kind == 'close':
        complete_horizon(r, 0, '2026-10-01')
        path['horizons'][0]['close'] = 0.
    elif kind == 'extreme_date':
        complete_horizon(r, 0, '2026-10-01')
        path['horizons'][0]['mfe_trade_date'] = 'bad'
    elif kind == 'complete_mismatch': r['complete'] = True
    elif kind == 'unknown_event': r['events'][0]['kind'] = 'bad'
    elif kind == 'duplicate_event':
        r['events'][0]['kind'] = 'interval_low'
        r['events'].append(deepcopy(r['events'][0]))
    elif kind == 'tie_order':
        r['events'][0]['kind'] = 'interval_low'
        r['events'].append({**r['events'][0], 'kind': 'interval_high'})
    elif kind == 'event_at_mention': r['events'][0]['at'] = '2026-09-30T15:00:00+08:00'
    elif kind == 'event_future': r['events'][0]['at'] = '2026-10-10T15:00:00+08:00'
    else: r['events'].append({**r['events'][0], 'kind': 'interval_high', 'at': '2026-10-08T15:00:00+08:00'})
    fail(code, lambda: cases.append_result(tmp_path, r))


def test_return_tolerance(tmp_path):
    r = a3_result(tmp_path)
    complete_horizon(r, 0, '2026-10-01')
    r['path_snapshot']['horizons'][0]['close_return'] = .1 + 5e-13
    cases.append_result(tmp_path, r)


def test_legacy_copied_case_store(tmp_path):
    copied = tmp_path / 'case_store'
    shutil.copytree(V2 / 'case_store', copied)
    case_id = 'yuncong_20260109_002025_000632'
    record = cases.load_restore_input(copied, case_id, 1)
    assert cases.load_results(copied, case_id, 1)
    v = ChartView.from_dict(json.loads((copied / 'inputs/daily_view.json').read_text(encoding='utf-8-sig')))
    assert v.identity() == record['view_identities'][0]


def test_legacy_f2_copied_views(tmp_path):
    paths = list(F2.glob('multi.*.view.json'))
    assert paths, 'Required frozen F2 regression views are missing'
    for path in paths:
        copied = tmp_path / path.name
        shutil.copyfile(path, copied)
        payload = json.loads(copied.read_text(encoding='utf-8-sig'))
        v = ChartView.from_dict(payload)
        assert v.to_dict() == payload
