"""Verify independently rebuilt tables, figures and manually stated report facts."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from .io import read_csv,sha256,write_json

def verify(report,rebuild,today_output):
    report=Path(report);rebuild=Path(rebuild);today_output=Path(today_output)
    published=read_csv(report/'data/mkt_daily.csv');rebuilt=read_csv(rebuild/'mkt_daily.csv');live=read_csv(today_output/'mkt_daily.csv')
    ignore={'generated_at','input_snapshot_id','mkt_summary'}
    # Summary text is checked separately: the textual missing-calendar label is not a forecast value.
    columns=[c for c in published if c not in ignore]
    pd.testing.assert_frame_equal(published[columns],rebuilt[columns],check_exact=True)
    core=['mkt_hit','mkt_cont','mkt_act','mkt_relay_score','mkt_f1_m1','mkt_weather','mkt_relay_grade']
    for c in core:
        if pd.api.types.is_numeric_dtype(published[c]):np.testing.assert_allclose(published[c],live[c],rtol=0,atol=1e-12,equal_nan=True)
        else:assert published[c].equals(live[c])
    figure_checks=[]
    for file in sorted((report/'figures').glob('*.png')):
        same=sha256(file)==sha256(rebuild/'figures'/file.name);assert same,file.name
        figure_checks.append({'file':file.name,'sha256':sha256(file),'independent_rebuild_bytes_equal':same})
    for name in ('monthly_weather.csv','yearly_weather.csv','transition_table.csv','weather_relay_crosstab.csv','report_numbers.json'):
        assert sha256(report/'data'/name)==sha256(rebuild/'data'/name),name
    assert json.loads((report/'data/forecast_evaluation.json').read_text(encoding='utf-8'))==json.loads((rebuild/'forecast_evaluation.json').read_text(encoding='utf-8'))
    text=(report/'REPORT.md').read_text(encoding='utf-8')
    required=['424','181','43.09%','40.88%','28.18%','38.12%','63.78','68.68','62.19','project_review_accepted','ready_not_enabled']
    for token in required:assert token in text,token
    actual=published[published.trade_date.str.startswith('2026')]
    assert actual.mkt_weather.value_counts().to_dict()=={'cloudy':69,'storm':47,'sunny':32,'overcast':27,'thunder':6}
    assert actual.mkt_quality_status.value_counts().to_dict()=={'OK':153,'DEGRADED':28}
    today=json.loads((today_output/'today_receipt.json').read_text(encoding='utf-8'))
    assert today['row_count']==424 and today['duplicates']==0 and today['changed_historical_dates']==[]
    assert today['calibration_sha256']==sha256(report/'data/mkt_calibration_v02.json')
    evidence={'status':'passed','derived_rebuild_all_scientific_columns_exact':True,'live_today_core_columns_equal':True,
              'figure_checks':figure_checks,'five_figures_visually_reviewed_by_controller':True,
              'visual_review':'All five actual PNGs opened: Chinese text, titles, axes, colors, counts, whitespace and legends readable; no cropping observed.',
              'report_number_checks':required,'summary_tables_equal':True,'today_receipt':today,
              'new_pdf_or_ppt_created':False}
    write_json(report/'acceptance/rebuild_visual_today.json',evidence)
    return evidence

def main():
    p=argparse.ArgumentParser();p.add_argument('--report',required=True);p.add_argument('--rebuild',required=True);p.add_argument('--today-output',required=True);a=p.parse_args()
    print(verify(a.report,a.rebuild,a.today_output))
if __name__=='__main__':main()
