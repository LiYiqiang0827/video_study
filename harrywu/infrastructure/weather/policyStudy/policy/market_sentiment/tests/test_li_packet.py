from pathlib import Path
import pandas as pd
import pytest
from pypdf import PdfReader
from policyStudy.policy.market_sentiment import li_packet
from policyStudy.policy.market_sentiment.io import read_csv
from policyStudy.policy.market_sentiment.run import REPO,parser
from policyStudy.policy.market_sentiment.li_review import FIXED_DATES

def source():
    return read_csv(REPO/'docs/research/mkt_weather_v02_20261005/data/mkt_daily.csv')

def test_li_packet_exact_ten_shuffled_blind_and_label_order(tmp_path):
    result=li_packet.create_li_packet(source(),tmp_path/'li')
    order=result['card_dates'];assert set(order)==set(FIXED_DATES) and len(order)==10
    assert order!=sorted(order) and order!=list(FIXED_DATES)
    labels=pd.read_csv(result['paths']['Li_labels'],keep_default_na=False)
    assert labels.trade_date.tolist()==order and labels.drop(columns='trade_date').eq('').to_numpy().all()
    assert not (tmp_path/'li/packets/Harry_labels.csv').exists()
    pages=PdfReader(result['paths']['cards_pdf']).pages
    assert len(pages)==10
    for day,page in zip(order,pages):
        text=page.extract_text();assert day in text
        assert '挨打（必填）' in text and '天气（选填）' in text and '整张跳过' in text
        assert not any(x in text for x in ('临界','随机','machine','mkt_','暴雨','雷阵雨','多云','阈值'))

def test_packet_preserves_filled_labels_and_stable_order(tmp_path):
    data=source()
    result=li_packet.create_li_packet(data,tmp_path)
    first=Path(result['paths']['Li_labels']).read_bytes()
    again=li_packet.create_li_packet(data.iloc[::-1],tmp_path)
    assert again['card_dates']==result['card_dates']
    assert Path(result['paths']['Li_labels']).read_bytes()==first
    path=Path(result['paths']['Li_labels']);frame=pd.read_csv(path,keep_default_na=False)
    frame.loc[0,'notes']='Do not overwrite';frame.to_csv(path,index=False)
    filled=path.read_bytes()
    with pytest.raises(FileExistsError,match='Preserve'):li_packet.create_li_packet(data,tmp_path)
    assert path.read_bytes()==filled

def test_cli_has_one_reviewer_and_no_dual_rater_gate():
    args=parser().parse_args(['evaluate-human','--daily','daily.csv','--li','Li.csv','--output','out'])
    assert not hasattr(args,'harry') and not hasattr(args,'threshold_revision_count')
    assert args.revision_ledger is None
    assert parser().parse_args(['li-pack','--daily','daily.csv','--output','out']).command=='li-pack'
