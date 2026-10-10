"""Risk checks for CLI visibility controls, separate from plotting tests."""
import json
from pathlib import Path
import pytest
from chart_tool.cli import parser, make_request, zoom_view
from chart_tool.contracts import ChartError, ChartView

FIXTURE = Path('C:/Project Rocket/outputs/chart_tool_development_20261009/E/views_synthetic/daily.json')

def test_restore_requires_actual_cutoff_evidence():
    args = parser().parse_args(['chart','000001.SZ','--stage','restore','--out','unused.png'])
    with pytest.raises(ChartError) as caught:
        make_request(args)
    assert caught.value.code == 'CUTOFF_UNRESOLVED'

def test_zoom_cannot_reveal_future_or_change_anchor():
    original = ChartView.from_dict(json.loads(FIXTURE.read_text()))
    before = original.to_dict()
    zoom = zoom_view(original, start='2026-09-24', end='2099-01-01')
    assert len(zoom.bars) == 1
    assert zoom.bars.bar_end.max() == '2026-09-24T10:07:00+08:00'
    assert zoom.bars.high.max() < 999
    assert zoom.metadata['factor_lineage'] == original.metadata['factor_lineage']
    assert zoom.metadata['requested_cutoff'] == original.metadata['requested_cutoff']
    assert original.to_dict() == before
    assert zoom_view(original, start='2099-01-01').bars.empty

def test_bad_zoom_range_rejected():
    view = ChartView.from_dict(json.loads(FIXTURE.read_text()))
    with pytest.raises(ChartError):
        zoom_view(view,start='2026-09-25',end='2026-09-24')

def test_day_precision_zoom():
    path = FIXTURE.parents[2] / 'integration/daily_actual_source.view.json'
    view = ChartView.from_dict(json.loads(path.read_text()))
    zoom = zoom_view(view,start='2026-08-01')
    assert zoom.bars.trade_date.min() >= '2026-08-01'
    assert zoom.bars.trade_date.max() == '2026-09-30'
    assert not zoom.metadata['incomplete']


def test_complete_cutoff_source_is_preserved(tmp_path):
    source = {'kind':'screen_clock','confidence':'high','evidence_refs':['frame.jpg'],
              'candidates':[{'cutoff':'2026-01-09T09:22:33+08:00','evidence_ref':'frame.jpg'},
                            {'cutoff':'2026-01-09T09:23:00+08:00','evidence_ref':'offset'}],
              'calibration_segment':'s12'}
    path = tmp_path/'source.json'
    path.write_text(json.dumps(source),encoding='utf-8')
    args = parser().parse_args(['chart','002025.SZ','--stage','restore','--cutoff','2026-01-09T09:22:33+08:00',
                               '--cutoff-source-json',str(path),'--cutoff-evidence','ignored','--out','unused.png'])
    assert make_request(args).cutoff_source == source
    source['candidates'][0]['cutoff']='2026-01-09T09:20:00+08:00'
    path.write_text(json.dumps(source),encoding='utf-8')
    with pytest.raises(ChartError) as exc:
        make_request(args)
    assert exc.value.code=='CUTOFF_LEAK'


def test_v2_parameter_defaults_and_legacy_profile():
    args = parser().parse_args(['chart-multi','002025.SZ','--cutoff','2026-09-18T10:07:30+08:00','--out','x.png'])
    assert args.minute_sessions==5 and args.daily_bars==250
    assert args.minute_profile is None and args.minute_snapshot_dir is None
    legacy = parser().parse_args(['chart-multi','002025.SZ','--cutoff',args.cutoff,'--out','x.png',
                                  '--minute-profile','profile.json','--minute-snapshot-dir','source'])
    assert legacy.minute_profile==Path('profile.json')
    args = parser().parse_args(['chart','600990.SH','--out','x.png','--price-scale','log','--no-auto-width','--last-bars','200'])
    assert args.price_scale=='log' and args.no_auto_width and args.last_bars==200


def test_pixel_failure_exit_mapping(monkeypatch,capsys):
    from chart_tool import cli
    monkeypatch.setattr(cli,'run_chart',lambda *a,**kw:{'status':'pixel_check_failed','png':'x.png'})
    assert cli.main(['chart','600990.SH','--out','x.png'])==4
    assert json.loads(capsys.readouterr().out)['status']=='pixel_check_failed'


@pytest.mark.parametrize('code,status',[(0,'completed'),(3,'partial'),(2,'failed')])
def test_reveal_exit_mapping(monkeypatch,capsys,code,status):
    from chart_tool import cli, reveal
    monkeypatch.setattr(reveal,'run_reveal',lambda *a,**kw:({'status':status,'results':[]},code))
    assert cli.main(['case-reveal','--store','case-store'])==code
    assert json.loads(capsys.readouterr().out)['status']==status
