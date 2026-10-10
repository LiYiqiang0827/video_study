"""Fixed-ten-date, single-Li reference review with directional evidence only."""
from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import re
from typing import Any

import numpy as np
import pandas as pd

from .readings import SPEC


FIXED_DATES = (
    "2026-08-27", "2026-08-31", "2026-09-03", "2026-09-07", "2026-09-09",
    "2025-11-14", "2025-12-03", "2026-05-07", "2026-06-10", "2026-06-11",
)
DIMENSIONS = ("hit", "cont", "act")
LABELS = {"hit": ("轻", "中", "重"), "cont": ("差", "一般", "好"), "act": ("低", "中", "高")}
_SKIP = {"", "skip", "跳过"}
_DISAGREEMENT_COLUMNS = ["trade_date", "dimension", "machine_value", "machine_label", "li_label", "direction", "notes"]


def _date(value: Any) -> str:
    if pd.isna(value):
        raise ValueError("Missing trade_date")
    if isinstance(value, (date, datetime, pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).date().isoformat()
    text = str(value).strip()
    if re.fullmatch(r"\d{8}(?:\.0+)?", text):
        return datetime.strptime(text.split(".")[0], "%Y%m%d").date().isoformat()
    return date.fromisoformat(text).isoformat()


def _bands(dimension: str) -> tuple[float, float]:
    configured = SPEC["human_bins"]
    pair = configured[dimension] if isinstance(configured, dict) else configured
    if len(pair) != 2:
        raise ValueError(f"Invalid {dimension} machine band configuration")
    lower, upper = map(float, pair)
    if not np.isfinite([lower, upper]).all() or not 0 <= lower < upper <= 100:
        raise ValueError(f"Invalid {dimension} machine band configuration")
    return lower, upper


def _machine(value: Any, dimension: str) -> tuple[str | None, int | None]:
    if pd.isna(value) or value == "":
        return None, None
    score = float(value)
    if not np.isfinite(score):
        return None, None
    if not 0 <= score <= 100:
        raise ValueError(f"Invalid mkt_{dimension} reading outside 0..100")
    lower, upper = _bands(dimension)
    rank = 0 if score < lower else 1 if score < upper else 2
    return LABELS[dimension][rank], rank


def _revision_counts(value: dict | None) -> dict[str, int]:
    if value is None:
        return dict.fromkeys(DIMENSIONS, 0)
    if not isinstance(value, dict) or set(value) - set(DIMENSIONS):
        raise ValueError("threshold_revision_counts accepts only hit/cont/act keys")
    result = dict.fromkeys(DIMENSIONS, 0)
    for dimension, count in value.items():
        if isinstance(count, (bool, np.bool_)) or not isinstance(count, (int, np.integer)) or count not in (0, 1):
            raise ValueError(f"{dimension} revision count must be integer 0 or 1")
        result[dimension] = int(count)
    return result


def _read_labels(li_path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(li_path, dtype=str, keep_default_na=False)
    required = {"trade_date", *DIMENSIONS}
    if not required.issubset(frame):
        raise ValueError(f"Li labels missing core columns: {sorted(required - set(frame))}")
    for optional in ("weather", "notes"):
        if optional not in frame:
            frame[optional] = ""
    frame = frame[["trade_date", *DIMENSIONS, "weather", "notes"]].copy()
    for column in frame:
        frame[column] = frame[column].str.strip()
    empty_date = frame.trade_date.eq("")
    if frame.loc[empty_date].drop(columns="trade_date").ne("").to_numpy().any():
        raise ValueError("Li annotations require a date")
    frame = frame.loc[~empty_date].copy()
    frame["trade_date"] = frame.trade_date.map(_date)
    if frame.trade_date.duplicated().any():
        raise ValueError("Li labels contain duplicate dates")
    outside = sorted(set(frame.trade_date) - set(FIXED_DATES))
    if outside:
        raise ValueError(f"Li labels outside fixed ten dates: {outside}")
    for dimension in DIMENSIONS:
        frame[dimension] = frame[dimension].map(lambda text: "" if text.casefold() in _SKIP else text)
        invalid = frame[dimension].ne("") & ~frame[dimension].isin(LABELS[dimension])
        if invalid.any():
            raise ValueError(f"Invalid Li {dimension} labels: {frame.loc[invalid, dimension].tolist()}")
    aliases = {**{code: code for code in SPEC["weather_order"]},
               **{label: code for code, label in SPEC["weather_labels"].items()}, **{skip: "" for skip in _SKIP}}
    frame["weather"] = frame.weather.map(lambda text: "" if text.casefold() in _SKIP else text)
    if not frame.weather.isin(aliases).all():
        raise ValueError("Invalid Li weather label")
    frame["weather"] = frame.weather.map(aliases)
    return frame.set_index("trade_date").reindex(FIXED_DATES).fillna("")


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


def evaluate_li_labels(daily: pd.DataFrame, li_path: str | Path, output_dir: str | Path,
                       threshold_revision_counts: dict | None = None) -> dict:
    """Record observations; return evidence eligibility without revising anything.

    ``basic_consistency`` uses seven matches in the fixed ten requested days.
    It is True as soon as seven are observed, False when all ten comparisons
    are observable but fewer than seven match, and None while unresolved.
    Missing/omitted labels and missing machine readings are never disagreements.
    """
    revision_counts = _revision_counts(threshold_revision_counts)
    required = {"trade_date", *(f"mkt_{dimension}" for dimension in DIMENSIONS)}
    if not required.issubset(daily):
        raise ValueError(f"daily missing required fields: {sorted(required - set(daily))}")
    source = daily.copy(deep=True)
    source["trade_date"] = source.trade_date.map(_date)
    if source.trade_date.duplicated().any():
        raise ValueError("daily contains duplicate dates")
    absent = [day for day in FIXED_DATES if day not in set(source.trade_date)]
    if absent:
        raise ValueError(f"Fixed review dates absent from daily: {absent}")
    source = source.set_index("trade_date").loc[list(FIXED_DATES)]
    labels = _read_labels(li_path)
    dimensions, disagreements = {}, []
    for dimension in DIMENSIONS:
        labeled_n = matches = evaluable_n = unavailable_n = higher_n = lower_n = 0
        unavailable_dates = []
        for day in FIXED_DATES:
            human = labels.at[day, dimension]
            machine_label, machine_rank = _machine(source.at[day, f"mkt_{dimension}"], dimension)
            if not human:
                continue
            labeled_n += 1
            if machine_rank is None:
                unavailable_n += 1
                unavailable_dates.append(day)
                continue
            evaluable_n += 1
            human_rank = LABELS[dimension].index(human)
            if machine_rank == human_rank:
                matches += 1
            else:
                direction = "higher" if machine_rank > human_rank else "lower"
                higher_n += direction == "higher"
                lower_n += direction == "lower"
                disagreements.append({"trade_date": day, "dimension": dimension,
                                      "machine_value": source.at[day, f"mkt_{dimension}"],
                                      "machine_label": machine_label, "li_label": human,
                                      "direction": direction, "notes": labels.at[day, "notes"]})
        eligible = [direction for direction, count in (("higher", higher_n), ("lower", lower_n)) if count >= 3]
        used = revision_counts[dimension]
        allowed = bool(eligible) and used == 0
        reason = ("dimension_revision_already_used" if used else "same_direction_evidence_ready_for_root_review" if eligible
                  else "no_evaluable_labels" if evaluable_n == 0 else "no_direction_has_three_valid_disagreements")
        if unavailable_n:
            reason += ";missing_machine_not_directional_evidence"
        dimensions[dimension] = {"matches": matches, "total_requested": len(FIXED_DATES),
                                 "labeled_n": labeled_n, "evaluable_n": evaluable_n,
                                 "missing_n": len(FIXED_DATES) - labeled_n,
                                 "unavailable_machine_n": unavailable_n,
                                 "unavailable_machine_dates": unavailable_dates,
                                 "basic_consistency": True if matches >= 7 else False if evaluable_n == 10 else None,
                                 "higher_disagreement_n": int(higher_n), "lower_disagreement_n": int(lower_n),
                                 "eligible_directions": eligible, "revision_used": used,
                                 "adjustment_allowed": allowed, "reason": reason}
    weather_matches = weather_n = weather_labeled_n = weather_unavailable_n = 0
    for day in FIXED_DATES:
        human = labels.at[day, "weather"]
        if not human:
            continue
        weather_labeled_n += 1
        machine_weather = source.at[day, "mkt_weather"] if "mkt_weather" in source else None
        if pd.isna(machine_weather) or machine_weather == "":
            weather_unavailable_n += 1
            continue
        if machine_weather not in SPEC["weather_order"]:
            raise ValueError(f"Invalid machine weather on {day}")
        weather_n += 1
        weather_matches += machine_weather == human
        if machine_weather != human:
            disagreements.append({"trade_date": day, "dimension": "weather", "machine_value": None,
                                  "machine_label": SPEC["weather_labels"][machine_weather],
                                  "li_label": SPEC["weather_labels"][human], "direction": "unranked",
                                  "notes": labels.at[day, "notes"]})
    incomplete = [day for day in FIXED_DATES if any(not labels.at[day, dimension] for dimension in DIMENSIONS)]
    partially_filled = [day for day in incomplete if any(labels.at[day, dimension] for dimension in DIMENSIONS)]
    blank = [day for day in FIXED_DATES if all(not labels.at[day, dimension] for dimension in DIMENSIONS)]
    core_labeled_n = sum(dimensions[dimension]["labeled_n"] for dimension in DIMENSIONS)
    status = "awaiting_labels" if core_labeled_n == 0 else "partial_labels" if incomplete else "review_recorded"
    output = Path(output_dir)
    paths = {"summary": str(output / "human_evaluation.json"), "metrics": str(output / "agreement_metrics.csv"),
             "disagreements": str(output / "disagreements.csv"), "readme": str(output / "README.md")}
    summary = {"reference_only": True, "status": status, "reviewer": "Li", "fixed_dates": list(FIXED_DATES),
               "total_requested": len(FIXED_DATES), "reference_match_count": 7,
               "dimensions": dimensions,
               "weather": {"matches": int(weather_matches), "denominator": weather_n, "labeled_n": weather_labeled_n,
                           "unavailable_machine_n": weather_unavailable_n},
               "incomplete_dates": incomplete, "partially_labeled_dates": partially_filled, "blank_or_skipped_dates": blank,
               "incomplete_details": [{"trade_date": day, "missing_dimensions": [dimension for dimension in DIMENSIONS if not labels.at[day, dimension]]} for day in incomplete],
               "threshold_revision_counts": revision_counts,
               "machine_band_cutpoints": {dimension: list(_bands(dimension)) for dimension in DIMENSIONS},
               "thresholds_changed": False, "machine_scores_changed": False, "paths": paths}
    output.mkdir(parents=True, exist_ok=True)
    (output / "human_evaluation.json").write_text(json.dumps(_safe(summary), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    pd.DataFrame([{"dimension": dimension, **{key: value for key, value in result.items() if key not in ("eligible_directions", "unavailable_machine_dates")},
                   "eligible_directions": ";".join(result["eligible_directions"])} for dimension, result in dimensions.items()]).to_csv(output / "agreement_metrics.csv", index=False, encoding="utf-8")
    order = {day: index for index, day in enumerate(FIXED_DATES)}
    detail = pd.DataFrame(disagreements, columns=_DISAGREEMENT_COLUMNS)
    if len(detail):
        detail = detail.assign(_order=detail.trade_date.map(order)).sort_values(["_order", "dimension"]).drop(columns="_order")
    detail.to_csv(output / "disagreements.csv", index=False, encoding="utf-8")
    (output / "README.md").write_text("# 李老师固定10日对照记录\n\n挨打、延续、活跃分别记录机器与李老师一致的天数。固定10日中至少7日一致仅作基本一致的参考，不设工程验收或停止工作的硬门槛。空白、跳过和机器缺失不算分歧，也不会把参考分母从10日缩小。已观察到7日一致时参考为真；10日都可比较且不足7日时为假；其余未定为null。部分填写日期仍保留各维已填的有效观察。\n\n某读数至少3个有效日期出现同方向分歧（机器档位较高或较低，分别统计）时，才具有总控复核调整该维分档的证据资格。相反方向不合并；机器缺失不提供方向证据。该读数已使用一次修订时不再允许调整。程序仅记录传入的逐维次数，既不更改阈值，也不更改分数。weather和notes可空，天气一致数仅作补充。\n", encoding="utf-8")
    return _safe(summary)
