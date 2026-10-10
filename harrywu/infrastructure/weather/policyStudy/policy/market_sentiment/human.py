"""Optional random/nominated blind packets and shared label helpers."""
from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import random
import re
from typing import Any

import numpy as np
import pandas as pd

from .readings import SPEC

WEATHER_ORDER = tuple(SPEC["weather_order"])
WEATHER_LABELS = dict(SPEC["weather_labels"])
DIMENSIONS = ("hit", "cont", "act")
BAND_LABELS = {"hit": ("轻", "中", "重"), "cont": ("差", "一般", "好"), "act": ("低", "中", "高")}
LABEL_COLUMNS = ["trade_date", "hit", "cont", "act", "weather", "notes"]
# This is a whitelist, never a prefix selection: scores and machine explanations
# also begin with mkt_, and must not leak into a packet.
NEUTRAL_FIELDS = {
    "mkt_eligible_n": ("合格股票数", "只"),
    "mkt_up_n": ("涨停数", "只"), "mkt_down_n": ("跌停数", "只"),
    "mkt_broken_n": ("炸板数", "只"), "mkt_max_height": ("最高连板", "板"),
    "mkt_turnover_cny": ("全市场成交额", "元"),
    "mkt_ma20_cny": ("此前20交易日成交均额（不含当日）", "元"),
    "mkt_ratio20": ("成交额/此前20日均额", "倍"),
    "mkt_index_sh_pct": ("上证指数涨跌幅", "%（百分点）"),
    "mkt_index_sz_pct": ("深证成指涨跌幅", "%（百分点）"),
    "mkt_index_cyb_pct": ("创业板指涨跌幅", "%（百分点）"),
    "mkt_advance_n": ("上涨家数（平盘不计）", "只"),
    "mkt_advance_pct": ("上涨家数占比", "0–1"),
    **{f"mkt_height_{h}_n": (f"当日{h}板人数", "只") for h in range(1, 6)},
    "mkt_height_6plus_n": ("当日6板及以上人数", "只"),
    "mkt_ladder": ("有成员的连板档数/6", "0–1"),
    **{f"mkt_{g}_{q}": (f"昨日{label}{description}", unit)
       for g, label in (("all", "全部涨停群体"), ("chain", "≥2板群体"))
       for q, description, unit in (
           ("original_n", "原始人数", "只"), ("observed_n", "今日可观测人数", "只"),
           ("paused_n", "今日停牌人数", "只"), ("missing_n", "今日缺失人数", "只"),
           ("unobservable_n", "今日有记录但不可观测人数", "只"),
           ("drop_k", "今日大跌人数", "只"), ("drop_rate", "今日大跌比例（分母为可观测人数）", "0–1"))},
    **{f"mkt_promo_h{h}_{q}": (f"昨日{'≥4' if h == 4 else h}板{'今日晋级数' if q == 'k' else '可观测晋级分母'}", "只")
       for h in range(1, 5) for q in ("k", "n")},
}


def _date(value: Any) -> str:
    if pd.isna(value):
        raise ValueError("Missing trade_date")
    if isinstance(value, (date, datetime, pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).date().isoformat()
    text = str(value).strip()
    if re.fullmatch(r"\d{8}(?:\.0+)?", text):
        return datetime.strptime(text.split(".")[0], "%Y%m%d").date().isoformat()
    return date.fromisoformat(text).isoformat()


def _daily(daily: pd.DataFrame) -> pd.DataFrame:
    required = {"trade_date", "mkt_weather", *(f"mkt_{d}" for d in DIMENSIONS)}
    if not required.issubset(daily.columns):
        raise ValueError(f"daily missing required columns: {sorted(required - set(daily.columns))}")
    result = daily.copy(deep=True)
    result["trade_date"] = result.trade_date.map(_date)
    if result.trade_date.duplicated().any():
        raise ValueError("daily has duplicate dates")
    invalid = result.mkt_weather.notna() & ~result.mkt_weather.isin(WEATHER_ORDER) & result.mkt_weather.ne("")
    if invalid.any():
        raise ValueError("daily has unknown weather codes")
    return result.sort_values("trade_date").reset_index(drop=True)


def machine_band(value: Any, dimension: str) -> str | None:
    """Use the current frozen configuration's bands; missing has no label."""
    if pd.isna(value) or not np.isfinite(float(value)):
        return None
    score = float(value)
    if not 0 <= score <= 100:
        raise ValueError("A machine reading must lie in 0..100")
    bins = SPEC["human_bins"]
    lower, upper = bins[dimension] if isinstance(bins, dict) else bins
    return BAND_LABELS[dimension][0 if score < lower else 1 if score < upper else 2]


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_safe(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8")


def _nominations(value: Any) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame(columns=["trade_date", "nominated_by", "notes"])
    if isinstance(value, (str, Path)):
        frame = pd.read_csv(value, dtype=str, keep_default_na=False)
    elif isinstance(value, pd.DataFrame):
        frame = value.copy(deep=True)
    else:
        frame = pd.DataFrame(value)
    if "trade_date" not in frame:
        raise ValueError("nominations require trade_date")
    frame = frame.loc[frame.trade_date.notna() & frame.trade_date.astype(str).str.strip().ne("")].copy()
    frame["trade_date"] = frame.trade_date.map(_date)
    return frame


def _format(value: Any, field: str) -> str:
    if pd.isna(value):
        return "缺失"
    if field.endswith("_pct") and field not in ("mkt_index_sh_pct", "mkt_index_sz_pct", "mkt_index_cyb_pct"):
        return f"{float(value):.2%}"
    if field.endswith("drop_rate") or field == "mkt_ladder":
        return f"{float(value):.2%}"
    if field.endswith("_cny"):
        return f"{float(value) / 1e12:.4f} 万亿元"
    if field.endswith("_n") or field.endswith("_k") or field == "mkt_max_height":
        return str(int(value))
    return f"{float(value):.4f}"


def create_packets(daily: pd.DataFrame, output_dir: str | Path, nominations: Any = None, seed: int = 20261005) -> dict:
    """Write date-sorted neutral facts, independent blank labels and private key."""
    source = _daily(daily)
    nominees = _nominations(nominations)
    duplicate_nominations_n = int(nominees.trade_date.duplicated().sum())
    nominees = nominees.drop_duplicates("trade_date", keep="first").sort_values("trade_date")
    unknown = sorted(set(nominees.trade_date) - set(source.trade_date))
    if unknown:
        raise ValueError(f"Nominated dates absent from daily: {unknown}")
    nominated = set(nominees.trade_date)
    rng = random.Random(seed)
    pools = {code: source.loc[source.mkt_weather.eq(code), "trade_date"].tolist() for code in WEATHER_ORDER}
    initial = {code: rng.sample(pools[code], min(3, len(pools[code]))) for code in WEATHER_ORDER}
    draws, replacements, strata = {}, {}, {}
    for code in WEATHER_ORDER:
        kept = [day for day in initial[code] if day not in nominated]
        overlaps = [day for day in initial[code] if day in nominated]
        available = [day for day in pools[code] if day not in nominated and day not in kept]
        replacements[code] = rng.sample(available, min(len(overlaps), len(available)))
        draws[code] = kept + replacements[code]
        strata[code] = {"candidate_n": len(pools[code]), "requested_random_n": 3,
                       "initial_random_n": len(initial[code]), "overlap_with_nominations_n": len(overlaps),
                       "replacement_n": len(replacements[code]), "final_random_n": len(draws[code]),
                       "random_shortfall_n": 3 - len(draws[code])}
    selected_dates = sorted(nominated | {day for group in draws.values() for day in group})
    selected = source[source.trade_date.isin(selected_dates)].copy()
    facts = selected.reindex(columns=["trade_date", *NEUTRAL_FIELDS])
    packets = Path(output_dir) / "packets"
    private = Path(output_dir) / "private_key"
    existing_selection = private / "selection_manifest.csv"
    if nominations is None and existing_selection.exists():
        frozen_dates = pd.read_csv(existing_selection, dtype=str).trade_date.tolist()
        if frozen_dates != selected_dates:
            raise ValueError("Preserve existing selected dates; use a new packet directory for another sample")
    label_paths = {name: packets / f"{name}_labels.csv" for name in ("Li",)}
    # Rebuilding a blank packet is safe; overwriting a person's work is not.
    for path in label_paths.values():
        if path.exists():
            existing = pd.read_csv(path, dtype=str, keep_default_na=False)
            content = existing.drop(columns="trade_date", errors="ignore")
            if content.astype(str).apply(lambda column: column.str.strip().ne("")).to_numpy().any():
                raise FileExistsError(f"Preserve existing completed/annotated labels: {path}")
    packets.mkdir(parents=True, exist_ok=True)
    private.mkdir(parents=True, exist_ok=True)
    _csv(private / "audit_raw_facts.csv", facts)
    blanks = pd.DataFrame({column: selected_dates if column == "trade_date" else [""] * len(selected_dates) for column in LABEL_COLUMNS})
    for path in label_paths.values():
        _csv(path, blanks)
    nomination_template = packets / "nominations_template.csv"
    if not nomination_template.exists():
        _csv(nomination_template, pd.DataFrame(columns=["trade_date", "nominated_by", "notes"]))
    from .cards import write_cards
    card_paths = write_cards(source, selected_dates, packets)
    # Old wide blind materials become private audit evidence. Paths are fixed
    # children of this explicit output directory, never computed glob moves.
    for filename in ("facts.csv", "facts.md"):
        old = packets / filename
        if old.exists():
            old.replace(private / f"archived_{filename}")
    glossary = ["# 可选随机卡片与提名材料", "", "本目录原15张随机卡片保留为可选参考，无需填写；当前必需工作仅为用户指定的10日李老师卡片，入口见 ../li10/。", "",
                "如自愿补充，可阅读cards.pdf或cards.html并填写Li_labels.csv。历史Harry_labels.csv仅为旧版留档，不再要求第二位标注者。", "",
                "hit：轻/中/重；cont：差/一般/好；act：低/中/高。weather和notes选填，允许留空或跳过。", "",
                "卡片每项并列2025 P10/中位/P90，比例均为可观测分母的原始k/n，不平滑。金额单位万亿元，指数及比例为百分比。", "",
                "nominations_template.csv及human-pack入口继续保留为可选功能，不要求补足日期，也不纳入当前10日参考评估。"]
    (packets / "README.md").write_text("\n".join(glossary) + "\n", encoding="utf-8")
    key = selected.reindex(columns=["trade_date", "mkt_hit", "mkt_cont", "mkt_act", "mkt_weather"])
    for dimension in DIMENSIONS:
        key[f"machine_{dimension}"] = key[f"mkt_{dimension}"].map(lambda value: machine_band(value, dimension))
    key["machine_weather_label"] = key.mkt_weather.map(WEATHER_LABELS)
    _csv(private / "machine_key.csv", key)
    sample_rows = []
    for day in selected_dates:
        code = source.loc[source.trade_date.eq(day), "mkt_weather"].iloc[0]
        sample_rows.append({"trade_date": day, "machine_weather": code,
                            "selection": "nominated" if day in nominated else "replacement" if any(day in group for group in replacements.values()) else "random",
                            "overlapped_initial_random": any(day in group for group in initial.values()) and day in nominated})
    _csv(private / "selection_manifest.csv", pd.DataFrame(sample_rows, columns=["trade_date", "machine_weather", "selection", "overlapped_initial_random"]))
    _csv(private / "nominations_used.csv", nominees)
    paths = {**card_paths, "facts_csv": str(private / "audit_raw_facts.csv"),
             "Li_labels": str(label_paths["Li"]),
             "nominations_template": str(nomination_template), "private_key": str(private / "machine_key.csv"),
             "selection_manifest": str(private / "selection_manifest.csv"), "sampling_summary": str(private / "sampling_summary.json")}
    summary = {"status": "optional_ready", "count": len(selected_dates),
               "counts": {"total": len(selected_dates), "random": sum(len(group) for group in draws.values()), "nominated": len(nominated)},
               "seed": seed, "required": False, "duplicate_nominations_n": duplicate_nominations_n,
               "strata": strata, "missing_neutral_fields": [field for field in NEUTRAL_FIELDS if field not in source],
               "paths": paths, "human_labels_filled": False}
    _json(private / "sampling_summary.json", summary)
    (private / "README.md").write_text("# 隔离机器答案与历史抽样依据\n\n仅作审计，不放入盲标材料。当前李老师10日包见../li10/packets；原随机卡片可选，无需第二位标注者。\n", encoding="utf-8")
    (Path(output_dir) / "README.md").write_text(f"# 可选随机与提名材料\n\n当前共{len(selected_dates)}个唯一日期，提名{len(nominated)}日；仅作为可选材料，不要求填写或补足。当前正式人工参考为李老师一人的指定10日包，使用li-pack生成。原随机/提名入口human-pack继续保留；已填标签不会被覆盖。\n", encoding="utf-8")
    return summary


def _labels(path: str | Path, name: str, available_dates: set[str]) -> pd.DataFrame:
    labels = pd.read_csv(path, dtype=str, keep_default_na=False)
    required = set(LABEL_COLUMNS) - {"notes"}
    if not required.issubset(labels.columns):
        raise ValueError(f"{name} labels missing columns: {sorted(required - set(labels.columns))}")
    if "notes" not in labels:
        labels["notes"] = ""
    labels = labels[LABEL_COLUMNS].copy()
    for column in LABEL_COLUMNS:
        labels[column] = labels[column].str.strip()
    blank_date = labels.trade_date.eq("")
    if labels.loc[blank_date, LABEL_COLUMNS[1:]].ne("").to_numpy().any():
        raise ValueError(f"{name} has annotations without a date")
    labels = labels.loc[~blank_date].copy()
    labels["trade_date"] = labels.trade_date.map(_date)
    if labels.trade_date.duplicated().any():
        raise ValueError(f"{name} labels have duplicate dates")
    unknown = sorted(set(labels.trade_date) - available_dates)
    if unknown:
        raise ValueError(f"{name} label dates absent from daily: {unknown}")
    for dimension in DIMENSIONS:
        invalid = labels[dimension].ne("") & ~labels[dimension].isin(BAND_LABELS[dimension])
        if invalid.any():
            raise ValueError(f"{name} invalid {dimension} label: {labels.loc[invalid, dimension].tolist()}")
    weather_aliases = {**{code: code for code in WEATHER_ORDER}, **{label: code for code, label in WEATHER_LABELS.items()}, "": ""}
    if not labels.weather.isin(weather_aliases).all():
        raise ValueError(f"{name} invalid weather label")
    labels["weather"] = labels.weather.map(weather_aliases)
    return labels.set_index("trade_date")


def _agreement(left: pd.Series, right: pd.Series) -> dict:
    available = left.notna() & left.ne("") & right.notna() & right.ne("")
    n = int(available.sum())
    hits = int(left.loc[available].eq(right.loc[available]).sum())
    return {"n": n, "hits": hits, "accuracy": hits / n if n else None}


def main() -> int:
    # Both module entry points use the current single-rater command contract.
    from .run import main as shared_main
    return shared_main()


if __name__ == "__main__":
    raise SystemExit(main())
