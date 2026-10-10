"""Ten-fact blind cards with unsmoothed 2025 daily reference quantiles."""
from __future__ import annotations
from html import escape
import base64
import hashlib
from pathlib import Path
import numpy as np
import pandas as pd

# All values here are raw quantities, never machine readings or decisions.
METRICS = {
    "up": ("涨停数", "只", "mkt_up_n", 1),
    "down": ("跌停数", "只", "mkt_down_n", 1),
    "broken": ("炸板数", "只", "mkt_broken_n", 1),
    "height": ("最高连板", "板", "mkt_max_height", 1),
    "all_k": ("昨日涨停今日大跌数", "只", "mkt_all_drop_k", 1),
    "all_n": ("昨日涨停今日可观测分母", "只", "mkt_all_observed_n", 1),
    "all_pct": ("昨日涨停今日大跌比例", "%", "all_raw_rate", 100),
    "chain_k": ("连板股今日大跌数", "只", "mkt_chain_drop_k", 1),
    "chain_n": ("连板股今日可观测分母", "只", "mkt_chain_observed_n", 1),
    "chain_pct": ("连板股今日大跌比例", "%", "chain_raw_rate", 100),
    **{f"h{h}_{q}": (f"昨日{'≥4' if h == 4 else h}板{'晋级数' if q == 'k' else '晋级分母' if q == 'n' else '晋级率'}",
                         "%" if q == "pct" else "只", f"h{h}_raw_rate" if q == "pct" else f"mkt_promo_h{h}_{q}", 100 if q == "pct" else 1)
       for h in range(1, 5) for q in ("k", "n", "pct")},
    "turnover": ("全市场成交额", "万亿元", "mkt_turnover_cny", 1e-12),
    "ratio": ("成交额/此前20交易日均额", "倍", "mkt_ratio20", 1),
    "advance_pct": ("上涨家数占比", "%", "advance_raw_rate", 100),
    "sh": ("上证指数涨跌幅", "%", "mkt_index_sh_pct", 1),
    "sz": ("深证成指涨跌幅", "%", "mkt_index_sz_pct", 1),
    "cyb": ("创业板指涨跌幅", "%", "mkt_index_cyb_pct", 1),
}
FONT_PATH = Path(__file__).resolve().parents[3] / "docs/research/mkt_weather_v02_20261005/reproduction/fonts/NotoSansSC-card.ttf"


def raw_values(daily: pd.DataFrame) -> pd.DataFrame:
    result = daily.copy(deep=True)
    def ratio(k, n):
        numerator = pd.to_numeric(result.get(k, pd.Series(np.nan, index=result.index)), errors="coerce")
        denominator = pd.to_numeric(result.get(n, pd.Series(np.nan, index=result.index)), errors="coerce")
        return numerator.div(denominator.where(denominator.gt(0)))
    for group in ("all", "chain"):
        result[f"{group}_raw_rate"] = ratio(f"mkt_{group}_drop_k", f"mkt_{group}_observed_n")
    for h in range(1, 5):
        result[f"h{h}_raw_rate"] = ratio(f"mkt_promo_h{h}_k", f"mkt_promo_h{h}_n")
    result["advance_raw_rate"] = ratio("mkt_advance_n", "mkt_eligible_n")
    values = pd.DataFrame(index=result.index)
    for metric, (_, _, column, scale) in METRICS.items():
        values[metric] = pd.to_numeric(result.get(column, pd.Series(np.nan, index=result.index)), errors="coerce") * scale
    return values.replace([np.inf, -np.inf], np.nan)


def reference_2025(daily: pd.DataFrame) -> pd.DataFrame:
    """Linear P10/P50/P90 over observed 2025 daily values in display units."""
    dates = pd.to_datetime(daily.trade_date.astype(str), format="mixed", errors="raise")
    visible = raw_values(daily).loc[dates.dt.year.eq(2025)]
    rows = []
    for metric, (label, unit, _, _) in METRICS.items():
        observations = visible[metric].dropna()
        quantiles = observations.quantile([.1, .5, .9], interpolation="linear")
        rows.append({"metric": metric, "label": label, "unit": unit,
                     "n_valid": len(observations), "n_missing": len(visible) - len(observations),
                     "p10": quantiles.get(.1, np.nan), "p50": quantiles.get(.5, np.nan), "p90": quantiles.get(.9, np.nan)})
    return pd.DataFrame(rows)


def _number(value, unit):
    if pd.isna(value):
        return "缺失"
    digits = 3 if unit == "万亿元" else 2 if unit in ("%", "倍") else 1
    text = f"{float(value):.{digits}f}"
    if unit in ("只", "板"):
        text = text.rstrip("0").rstrip(".")
    return text


def card_rows(row: pd.Series, references: pd.DataFrame) -> list[dict]:
    values = raw_values(pd.DataFrame([row])).iloc[0]
    refs = references.set_index("metric")
    def current(metric):
        unit = METRICS[metric][1]
        value = values[metric]
        return "缺失" if pd.isna(value) else _number(value, unit) + unit
    def triple(metric):
        unit = METRICS[metric][1]
        if metric not in refs.index:
            return "缺失"
        observation = refs.loc[metric]
        return " / ".join(_number(observation[q], unit) for q in ("p10", "p50", "p90")) + unit
    result = [{"item": label, "current": current(metric), "reference": triple(metric)}
              for metric, label in (("up", "涨停"), ("down", "跌停"), ("broken", "炸板"), ("height", "最高连板"))]
    for group, title in (("all", "昨日涨停\n今日大跌"), ("chain", "连板股\n今日大跌")):
        result.append({"item": title,
                       "current": f"{current(group+'_k')} / 可观察 {current(group+'_n')}\n比例 {current(group+'_pct')}",
                       "reference": f"大跌数 {triple(group+'_k')}\n分母 {triple(group+'_n')}\n比例 {triple(group+'_pct')}"})
    result.append({"item": "各板晋级", "current": "\n".join(
        f"{'≥4' if h == 4 else h}板: {_number(values[f'h{h}_k'], '只')}/{_number(values[f'h{h}_n'], '只')}，{current(f'h{h}_pct')}"
        for h in range(1, 5)), "reference": "\n".join(
        f"{'≥4' if h == 4 else h}板 k {triple(f'h{h}_k')}；n {triple(f'h{h}_n')}\n比例 {triple(f'h{h}_pct')}"
        for h in range(1, 5))})
    result.append({"item": "成交额\n及倍数", "current": f"{current('turnover')}\n20日均额的 {current('ratio')}",
                   "reference": f"成交 {triple('turnover')}\n倍数 {triple('ratio')}"})
    result.append({"item": "上涨家数\n占比", "current": current("advance_pct"), "reference": triple("advance_pct")})
    result.append({"item": "三大指数\n涨跌", "current": "\n".join(f"{label} {current(metric)}" for metric, label in (("sh", "上证"), ("sz", "深证"), ("cyb", "创业板"))),
                   "reference": "\n".join(f"{label} {triple(metric)}" for metric, label in (("sh", "上证"), ("sz", "深证"), ("cyb", "创业板")))})
    assert len(result) == 10
    return result


def write_cards(daily: pd.DataFrame, selected_dates: list[str], output_dir: str | Path, *,
                preserve_order=False, form_footer=None, title_text="独立标注 · 当日事实卡", font_path=None) -> dict:
    """Create self-contained PDF and offline HTML; no sampling takes place here."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    references = reference_2025(daily)
    references.to_csv(output / "reference_2025.csv", index=False, encoding="utf-8")
    source = daily.set_index("trade_date")
    reference_days = int(pd.to_datetime(daily.trade_date).dt.year.eq(2025).sum())
    if len(set(selected_dates)) != len(selected_dates):
        raise ValueError('Duplicate card dates')
    cards = [(day, card_rows(source.loc[day], references)) for day in (selected_dates if preserve_order else sorted(selected_dates))]
    footer = form_footer or "标注者：________　挨打：________　延续：________　活跃：________\n最接近的天气：________　备注：____________________________"
    introduction = f"2025参照取全年{reference_days}个交易日的可观测日值，按线性分位计算；列内顺序为 P10 / 中位数 / P90。所有比例均为原始比例，未平滑。"
    legend = "晋级 k/n 为晋级只数/可观测分母；连板股包括昨日所有2板及以上。零分母的比例为缺失。成交倍数对比此前20交易日均额（不含今日）；百分比与指数均标%。"
    font_file = Path(font_path) if font_path else FONT_PATH
    if not font_file.exists():
        raise FileNotFoundError(f"Bundled card font missing: {font_file}")
    # Self-contained across drives and when the blind packet is copied elsewhere.
    font_relative = 'data:font/ttf;base64,' + base64.b64encode(font_file.read_bytes()).decode('ascii')
    (output / 'FONT_LICENSE.txt').write_bytes(FONT_PATH.with_name('NotoSansSC-card-OFL.txt').read_bytes())
    css = """@font-face{font-family:Card;src:url('FONT')}*{box-sizing:border-box}body{font-family:Card,'Microsoft YaHei',sans-serif;margin:0;color:#172b42;background:#edf1f5}.card{width:210mm;min-height:297mm;padding:12mm;margin:10mm auto;background:white}h1{font-size:22px;margin:0 0 6px}.date{font-size:20px;font-weight:bold;margin:4px 0 10px}.intro,.legend{font-size:11px;line-height:1.55;color:#566579}table{border-collapse:collapse;width:100%;font-size:12px;table-layout:fixed}th{background:#e6edf5;text-align:left;font-size:11px;padding:8px}td{padding:7px 8px;border-bottom:1px solid #dce3eb;white-space:pre-line;vertical-align:middle;line-height:1.5}th:nth-child(1){width:6%}th:nth-child(2){width:15%}th:nth-child(3){width:34%}th:nth-child(4){width:45%}tr:nth-child(even){background:#f7f9fc}.legend{margin-top:12px}.blank{margin-top:12px;font-size:12px;line-height:2}@media print{@page{size:A4;margin:0}body{background:white}.card{margin:0;height:297mm;page-break-after:always}.card:last-child{page-break-after:auto}}""".replace("FONT", font_relative)
    sections = []
    for page, (day, rows) in enumerate(cards, 1):
        body = "".join(f"<tr><td>{i}</td><td>{escape(item['item'])}</td><td>{escape(item['current'])}</td><td>{escape(item['reference'])}</td></tr>" for i, item in enumerate(rows, 1))
        form_html = escape(footer).replace('\n','<br>')
        sections.append(f"<section class='card'><h1>{escape(title_text)}</h1><div class='date'>{day}</div><p class='intro'>{introduction}</p><table><thead><tr><th>序</th><th>事实</th><th>当日</th><th>2025常见范围：P10 / 中位 / P90</th></tr></thead><tbody>{body}</tbody></table><p class='legend'>{legend}</p><div class='blank'>{form_html}</div><p class='intro'>第 {page} / {len(cards)} 张</p></section>")
    (output / "cards.html").write_text(f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>独立标注事实卡</title><style>{css}</style><body>{''.join(sections)}</body></html>", encoding="utf-8")
    font = 'RocketCardSansSC_' + hashlib.sha256(font_file.read_bytes()).hexdigest()[:12]
    if font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font, str(font_file)))
    style = ParagraphStyle("card", fontName=font, fontSize=9, leading=12, textColor=colors.HexColor("#172b42"), wordWrap="CJK")
    title = ParagraphStyle("title", parent=style, fontSize=18, leading=23)
    subtitle = ParagraphStyle("date", parent=style, fontSize=15, leading=20)
    small = ParagraphStyle("small", parent=style, fontSize=8, leading=11, textColor=colors.HexColor("#566579"))
    paragraph = lambda text, chosen=style: Paragraph(escape(text).replace("\n", "<br/>"), chosen)
    story = []
    for page, (day, rows) in enumerate(cards, 1):
        story += [paragraph(title_text, title), paragraph(day, subtitle), Spacer(1, 6), paragraph(introduction, small), Spacer(1, 8)]
        data = [[paragraph(t, small) for t in ("序", "事实", "当日", "2025常见范围：P10 / 中位 / P90")]]
        data += [[paragraph(str(i)), paragraph(item["item"]), paragraph(item["current"]), paragraph(item["reference"])] for i, item in enumerate(rows, 1)]
        table = Table(data, colWidths=[24, 67, 173, A4[0] - 64 - 264], hAlign="LEFT")
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6edf5")), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fc")]), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("LINEBELOW", (0, 0), (-1, -1), .35, colors.HexColor("#dce3eb"))]))
        story += [table, Spacer(1, 10), paragraph(legend, small), Spacer(1, 10), paragraph(footer), Spacer(1, 6), paragraph(f"第 {page} / {len(cards)} 张", small)]
        if page < len(cards):
            story.append(PageBreak())
    SimpleDocTemplate(str(output / "cards.pdf"), pagesize=A4, leftMargin=32, rightMargin=32, topMargin=26, bottomMargin=26, title="独立标注事实卡", author="TradingAgent").build(story)
    return {"cards_html": str(output / "cards.html"), "cards_pdf": str(output / "cards.pdf"), "reference_2025": str(output / "reference_2025.csv")}
