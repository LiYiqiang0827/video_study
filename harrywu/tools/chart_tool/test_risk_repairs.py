"""Regression inputs independently reproduced by G-001; all writes use C fixtures."""
from copy import deepcopy
import hashlib
from pathlib import Path
import os
import subprocess
import threading
import json
from types import SimpleNamespace
import pytest
from chart_tool import cases
from chart_tool.cli import run_chart, _reveal, _export_restore
from chart_tool.contracts import ChartError, RenderOptions
from chart_tool.test_cases import make_record, freeze
from chart_tool.test_data import snapshot, request


def test_source_png_and_parquet_cannot_be_overwritten(tmp_path):
    source=tmp_path/'source'; snapshot(source)
    for name in ('600990.SH.daily.parquet','existing.png'):
        target=source/name
        if not target.exists(): target.write_bytes(b'SOURCE_SENTINEL')
        before=target.read_bytes()
        with pytest.raises(ChartError) as error:
            run_chart(request(),output_path=target,cache_dir=tmp_path/'cache',snapshot_dir=source,
                      options=RenderOptions(width_px=640,height_px=400))
        assert error.value.code=='OUTPUT_PATH'
        assert target.read_bytes()==before


@pytest.mark.parametrize('name',['result_artifacts/future.json','ReSuLt_ArTiFaCtS/future.json',
                                  'record.json','CONTEXT_BOUNDARY.txt','absolute'])
def test_only_relocatable_dedicated_input_refs_can_freeze(tmp_path,name):
    store=tmp_path/'store'; store.mkdir(); record=make_record(store)
    if name=='absolute':
        path=store/'inputs/transcript.txt'; reference=str(path)
    else:
        path=store/name; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('FUTURE_RESULT_SENTINEL',encoding='utf-8'); reference=name
    record['input_refs'][1].update(path=reference,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    record['observations']['seen']['evidence_refs']=[reference]
    with pytest.raises(ChartError) as error: freeze(store,record)
    assert error.value.code=='CASE_PATH'
    assert not (store/'restore'/record['case_id']/'revision-1/frozen.json').exists()


def test_hardlink_to_result_cannot_enter_inputs(tmp_path):
    store=tmp_path/'store'; store.mkdir(); record=make_record(store)
    future=store/'results/future.json'; future.parent.mkdir(); future.write_text('FUTURE_SENTINEL')
    alias=store/'inputs/result_alias.json'; os.link(future,alias)
    record['input_refs'][1].update(path='inputs/result_alias.json',sha256=hashlib.sha256(alias.read_bytes()).hexdigest())
    record['observations']['seen']['evidence_refs']=['inputs/result_alias.json']
    with pytest.raises(ChartError) as error: freeze(store,record)
    assert error.value.code=='CASE_PATH'


def test_result_junction_rejected_before_any_outside_write(tmp_path):
    store=tmp_path/'store'; store.mkdir(); record=make_record(store); freeze(store,record)
    anchor=record['view_identities'][0]['metadata']['factor_lineage']['anchor_value']
    source=tmp_path/'source'; snapshot(source,dates=['20260930','20261001'],factors=[('20260930',anchor),('20261001',anchor)])
    outside=tmp_path/'outside'; outside.mkdir(); link=store/'result_artifacts'
    command=f"New-Item -ItemType Junction -Path '{str(link).replace(chr(39),chr(39)*2)}' -Target '{str(outside).replace(chr(39),chr(39)*2)}' | Out-Null"
    completed=subprocess.run(['powershell.exe','-NoProfile','-Command',command],capture_output=True,text=True,timeout=30)
    assert completed.returncode==0,completed.stderr
    args=SimpleNamespace(store=store,case_id=record['case_id'],revision=1,
                         observed_through='2026-10-02T15:00:00+08:00',cache_dir=tmp_path/'cache',snapshot_dir=source,refresh=False)
    report = _reveal(args)
    assert report['results'][0]['status'] == 'failed'
    assert report['results'][0]['error']['code'] in {'CASE_PATH','OUTPUT_PATH'}
    assert list(outside.iterdir())==[]


def test_valid_export_preserves_every_input_hash(tmp_path):
    store=tmp_path/'store'; store.mkdir(); record=make_record(store); freeze(store,record)
    target=tmp_path/'fresh'
    report=_export_restore(SimpleNamespace(store=store,case_id=record['case_id'],revision=1,export_dir=target))
    assert report['results_read'] is False
    for ref in record['input_refs']:
        assert hashlib.sha256((target/ref['path']).read_bytes()).hexdigest()==ref['sha256']


def test_same_output_second_writer_rejected_before_png_sidecar_mixing(tmp_path,monkeypatch):
    from chart_tool import render
    source=tmp_path/'source'; snapshot(source)
    target=tmp_path/'same.png'; drawn=threading.Event(); resume=threading.Event()
    actual=render.render_png_with_geometry; errors=[]; reports=[]
    def paused(view,options,path):
        manifest=actual(view,options,path)
        drawn.set()
        assert resume.wait(10)
        return manifest
    monkeypatch.setattr(render,'render_png_with_geometry',paused)
    def first():
        try:
            reports.append(run_chart(request('2026-09-22T15:00:00+08:00'),output_path=target,
                                     cache_dir=tmp_path/'cache',snapshot_dir=source,
                                     options=RenderOptions(width_px=640,height_px=400)))
        except BaseException as error: errors.append(error)
    thread=threading.Thread(target=first); thread.start()
    try:
        assert drawn.wait(10)
        with pytest.raises(ChartError) as error:
            run_chart(request('2026-09-24T15:00:00+08:00'),output_path=target,
                      cache_dir=tmp_path/'cache',snapshot_dir=source)
        assert error.value.code=='OUTPUT_LOCKED'
    finally:
        resume.set(); thread.join(10)
    assert not thread.is_alive() and not errors
    payload=json.loads(target.with_suffix('.view.json').read_text())
    receipt=json.loads(target.with_suffix('.run.json').read_text())
    assert payload['metadata']['requested_cutoff']=='2026-09-22T15:00:00+08:00'
    assert receipt['png_sha256']==hashlib.sha256(target.read_bytes()).hexdigest()
    assert not target.with_suffix('.output.lock').exists()


@pytest.mark.parametrize('environment_key',['TRADING_AGENT_DATA_DIR','TRADING_AGENT_DERIVED_DATA_DIR'])
def test_cache_cannot_write_different_configured_source(tmp_path,monkeypatch,environment_key):
    from chart_tool.data import load_raw
    source=tmp_path/'snapshot'; snapshot(source)
    protected=tmp_path/'protected_source'; protected.mkdir()
    sentinel=protected/'access.jsonl'; sentinel.write_bytes(b'SOURCE_LOG_SENTINEL')
    monkeypatch.setenv(environment_key,str(protected))
    with pytest.raises(ChartError) as error:
        load_raw(request(),cache_dir=protected,snapshot_dir=source)
    assert error.value.code=='SOURCE_WRITE_SCOPE'
    assert sentinel.read_bytes()==b'SOURCE_LOG_SENTINEL'
    assert [p.name for p in protected.iterdir()]==['access.jsonl']


def test_failed_result_publication_does_not_block_retry(tmp_path):
    store=tmp_path/'store'; store.mkdir(); record=make_record(store); freeze(store,record)
    anchor=record['view_identities'][0]['metadata']['factor_lineage']['anchor_value']
    source=tmp_path/'source'; snapshot(source,dates=['20260930','20261001'],factors=[('20260930',anchor),('20261001',anchor)])
    args=SimpleNamespace(store=store,case_id=record['case_id'],revision=1,
                         observed_through='2026-10-02T15:00:00+08:00',cache_dir=tmp_path/'cache',snapshot_dir=source,refresh=False)
    observation=store/'result_artifacts'/record['case_id']/'revision-1/observation-1'
    with cases._writer(cases._root(store),record['case_id']):
        report = _reveal(args)
        assert report['results'][0]['status'] == 'failed'
        assert report['results'][0]['error']['code']=='CASE_LOCKED'
    assert not observation.exists()
    assert len(cases.list_pending_reveals(store))==1
    result=_reveal(args)
    assert result['results'][0]['rows']==1
    assert result['pending_after']==[]
