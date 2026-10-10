"""The requested fixed ten-day Li-only blind packet; no model-driven sampling."""
from pathlib import Path
import random
import pandas as pd
from .human import _daily, _csv, _json, LABEL_COLUMNS, machine_band, DIMENSIONS
from .cards import write_cards, FONT_PATH
from .li_review import FIXED_DATES

SHUFFLE_SEED = 20261005
LI_FONT = FONT_PATH.with_name('NotoSansSC-li.ttf')
FORM_FOOTER = ('挨打（必填）：轻 / 中 / 重\n'
               '延续（必填）：差 / 一般 / 好\n'
               '活跃（必填）：低 / 中 / 高\n'
               '最接近的天气（选填）：________________\n'
               '备注（选填）：________________________________\n'
               '整张跳过：[ ]')

def create_li_packet(daily, output_dir, seed=SHUFFLE_SEED):
    source=_daily(daily)
    missing=sorted(set(FIXED_DATES)-set(source.trade_date))
    if missing:raise ValueError(f'Required card dates missing: {missing}')
    order=list(FIXED_DATES)
    random.Random(seed).shuffle(order)
    out=Path(output_dir);packets=out/'packets';private=out/'private_key'
    labels_path=packets/'Li_labels.csv'
    if labels_path.exists():
        existing=pd.read_csv(labels_path,dtype=str,keep_default_na=False)
        if existing.drop(columns='trade_date',errors='ignore').apply(lambda s:s.str.strip().ne('')).to_numpy().any():
            raise FileExistsError(f'Preserve existing annotations: {labels_path}')
        if existing.trade_date.tolist()!=order:raise ValueError('Existing card order differs; use a new output directory')
    paths=write_cards(source,order,packets,preserve_order=True,form_footer=FORM_FOOTER,
                      title_text='李老师 · 当日事实卡',font_path=LI_FONT)
    labels=pd.DataFrame({c:order if c=='trade_date' else ['']*10 for c in LABEL_COLUMNS})
    _csv(labels_path,labels)
    key=source.set_index('trade_date').loc[order,['mkt_hit','mkt_cont','mkt_act','mkt_weather']].reset_index()
    for dimension in DIMENSIONS:key[f'machine_{dimension}']=key[f'mkt_{dimension}'].map(lambda v:machine_band(v,dimension))
    _csv(private/'machine_key.csv',key)
    _json(private/'card_order.json',{'seed':seed,'card_dates':order,'selection':'ten dates explicitly requested by user; shuffled independently of readings'})
    ledger=private/'threshold_revision_ledger.json'
    if not ledger.exists():_json(ledger,{d:0 for d in DIMENSIONS})
    (packets/'README.md').write_text('''# 李老师的10日事实卡

请按cards.pdf中的顺序逐张判断，每天一页；Li_labels.csv与卡片顺序相同。也可在PDF打印件上圈选后录入。参考范围为2025全年可观察原始日值的P10、中位数、P90。

三项主要标签为必填项，完成一张请填齐：

| 列 | 填写内容 |
|---|---|
| trade_date | 日期，保留原值 |
| hit | 挨打：轻 / 中 / 重 |
| cont | 延续：差 / 一般 / 好 |
| act | 活跃：低 / 中 / 高 |
| weather | 最接近的天气，选填：晴 / 多云 / 阴 / 雷阵雨 / 暴雨 |
| notes | 备注，选填 |

不确定的项目可以留空，也可整张跳过。程序会保留部分填写，单独列出未完成日期；空白不当作不一致。没有必需填写的额外日期或提名任务。

金额显示万亿元；指数为涨跌百分比。大跌事实为收盘较有效前收跌5%及以上或收于跌停，每只只计一次；连板群体包含昨日所有2板及以上。比例采用当天可观察分母，未平滑，零分母留缺失。晋级按昨日1/2/3/≥4板分组。
''',encoding='utf-8')
    (out/'README.md').write_text('''# 当前人工标注：李老师一人、固定10日

仅将packets交给李老师：10页乱序PDF、同序空白标签表和事实参照。该目录不含机器答案或日期类别提示。private_key为隔离答案、顺序审计和修订次数，标注前不需阅读。

逐读数统计机器与李老师一致天数。固定10天中至少7天一致记为基本一致，只作参考，不设整体通过门槛；空白不算不一致，也不缩小这个7/10参考基数。同一读数至少3个有效日均为机器档位偏高、或至少3日均为偏低，且该读数尚未修订，才允许总控依据分歧调整该读数分档阈值一次。相反方向不能相加。评估程序不自动修改阈值；记录每个读数的次数。

原15张随机卡片和提名入口保留为可选材料，无需填写，也不进入此次10日评估。
''',encoding='utf-8')
    return {'status':'ready_awaiting_li_labels','count':10,'paths':{**paths,'Li_labels':str(labels_path),
            'machine_key':str(private/'machine_key.csv'),'revision_ledger':str(ledger)},'seed':seed,'card_dates':order}
