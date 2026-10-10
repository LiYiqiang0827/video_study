"""Adapt frozen F1 tables to market-weather raw measurements.

``build_raw`` is a pure calculation: it neither reads paths nor changes its
inputs. F1 event identities, integer-cent prices, heights and observability are
already resolved upstream. Old columns remain as evidence; only ``trade_date``
is normalized to ISO. No F1 scores, parameters or calibration are recomputed.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import re
from typing import Iterable

import numpy as np
import pandas as pd


FIELD_MAPPING = {
    "mkt_eligible_n": "N",
    "mkt_up_n": "U",
    "mkt_down_n": "D",
    "mkt_broken_n": "Z",
    "mkt_turnover_cny": "amount",
    "mkt_ma20_cny": "amt_ma20",
    "mkt_ratio20": "ratio20",
    "mkt_log_ratio20": "lr20",
    "mkt_udens": "Udens",
    "mkt_ddens": "Ddens",
    "mkt_m1raw": "M1raw",
    "mkt_ladder": "ladder",
    "mkt_max_height": "max_h",
    "mkt_relay_score": "score5",
    "mkt_relay_grade": "grade5",
    "mkt_f1_m1": "M1",
    "mkt_source_quality": "quality_status",
    "mkt_source_reasons": "quality_reasons",
}
for _height in range(1, 5):
    for _quantity in ("k", "n"):
        FIELD_MAPPING[f"mkt_promo_h{_height}_{_quantity}"] = f"{_quantity}_h{_height}"

_EVENT_COLUMNS = ("ts_code", "eligible", "U", "height", "cC", "cP")
_COHORT_COLUMNS = (
    "ts_code", "prev_height", "found", "paused", "missing", "mid_obs",
    "cC", "cP", "Dn",
)
_EVENT_REUSED = (
    "mkt_eligible_n", "mkt_up_n", "mkt_down_n", "mkt_broken_n",
    "mkt_udens", "mkt_ddens", "mkt_ladder", "mkt_max_height",
)
_HEIGHT_FIELDS = [f"mkt_height_{height}_n" for height in range(1, 6)] + ["mkt_height_6plus_n"]
_GROUP_QUANTITIES = (
    "original_n", "observed_n", "paused_n", "missing_n", "unobservable_n",
    "drop_k", "drop_rate",
)


def _iso_date(value: object) -> str:
    if pd.isna(value):
        raise ValueError("A trading date cannot be missing")
    if isinstance(value, (pd.Timestamp, datetime, date, np.datetime64)):
        return pd.Timestamp(value).date().isoformat()
    text = str(value).strip()
    # CSV inference may represent an eight-digit integer date as a float.
    if re.fullmatch(r"\d{8}\.0+", text):
        text = text.split(".")[0]
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text).isoformat()
    # Timestamp objects serialized by CSV may carry a time component.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T].+", text):
        return pd.Timestamp(text).date().isoformat()
    raise ValueError(f"Unsupported trading date: {value!r}")


def _dates(frame: pd.DataFrame, name: str) -> pd.Series:
    candidates = [column for column in ("trade_date", "date") if column in frame]
    if not candidates:
        if frame.empty:
            return pd.Series(index=frame.index, dtype="object")
        raise ValueError(f"{name} requires trade_date or date")
    normalized = []
    for column in candidates:
        values = frame[column].astype(object)
        mapping = {value: _iso_date(value) for value in values.unique()}
        normalized.append(values.map(mapping))
    if len(normalized) == 2 and not normalized[0].equals(normalized[1]):
        raise ValueError(f"{name} trade_date and date disagree")
    return normalized[0]


def _require(frame: pd.DataFrame, columns: Iterable[str], name: str) -> None:
    missing = [column for column in columns if column not in frame]
    if missing and not frame.empty:
        raise ValueError(f"{name} missing columns: {', '.join(missing)}")


def _cents(frame: pd.DataFrame, name: str) -> tuple[pd.Series, pd.Series]:
    """Validate observed integer pennies before doing exact comparisons."""
    cents = []
    for column in ("cC", "cP"):
        numeric = pd.to_numeric(frame[column], errors="coerce")
        valid = np.isfinite(numeric) & numeric.gt(0) & numeric.eq(np.floor(numeric))
        valid &= numeric.le(np.iinfo(np.int64).max // 20)
        if not valid.all():
            raise ValueError(f"{name} observed {column} must contain positive integer cents")
        cents.append(numeric.astype(np.int64))
    return cents[0], cents[1]


def _expected(row: pd.Series, *columns: str) -> float | None:
    for column in columns:
        if column in row and pd.notna(row[column]):
            return float(row[column])
    return None


def _group_stats(cohort: pd.DataFrame) -> dict[str, float | int]:
    observed = cohort.mid_obs.eq(True)
    g = cohort.loc[observed]
    if len(g):
        close, reference = _cents(g, "cohorts")
        drop_k = int(((close * 20 <= reference * 19) | g.Dn.eq(True)).sum())
    else:
        drop_k = 0
    n = int(observed.sum())
    return {
        "original_n": len(cohort),
        "observed_n": n,
        "paused_n": int(cohort.paused.eq(True).sum()),
        "missing_n": int(cohort.missing.eq(True).sum()),
        "unobservable_n": int((cohort.found.eq(True) & ~observed).sum()),
        "drop_k": drop_k,
        "drop_rate": drop_k / n if n else np.nan,
    }


def build_raw(
    events: pd.DataFrame,
    cohorts: pd.DataFrame,
    formal_daily: pd.DataFrame,
    calendar: Iterable[object] | None = None,
) -> pd.DataFrame:
    """Return one ISO-date row per calendar day, retaining all old daily fields.

    The calendar defaults to the formal daily dates. A populated event day is
    distinct from a day absent from the event input: no limit-up events on a
    populated day gives genuine zero height counts. An absent/partial input day
    gives missing event-derived measurements. Empty cohorts are genuine empty
    groups only when F1's original cohort count is zero; absent rows for a
    nonempty or unknown group give missing measurements. Ratios with n=0 remain
    missing. Supplemental adapter quality fields disclose these distinctions.
    """
    daily = formal_daily.copy(deep=True)
    daily["trade_date"] = _dates(daily, "formal_daily")
    if daily.trade_date.duplicated().any():
        raise ValueError("formal_daily has duplicate trading dates")
    days = daily.trade_date.tolist() if calendar is None else [_iso_date(day) for day in calendar]
    if len(days) != len(set(days)):
        raise ValueError("calendar has duplicate trading dates")
    daily = daily.set_index("trade_date").reindex(sorted(days)).reset_index()

    _require(events, _EVENT_COLUMNS, "events")
    _require(cohorts, _COHORT_COLUMNS, "cohorts")
    # Copy only the columns used here, rather than millions of wide F1 rows.
    e = events.reindex(columns=_EVENT_COLUMNS).copy()
    e["trade_date"] = _dates(events, "events")
    c = cohorts.reindex(columns=_COHORT_COLUMNS).copy()
    c["trade_date"] = _dates(cohorts, "cohorts")
    for frame, name in ((e, "events"), (c, "cohorts")):
        if frame.duplicated(["trade_date", "ts_code"]).any():
            raise ValueError(f"{name} has duplicate date/security rows")
    if len(c):
        heights = pd.to_numeric(c.prev_height, errors="coerce")
        if not (np.isfinite(heights) & heights.ge(1) & heights.eq(np.floor(heights))).all():
            raise ValueError("cohorts prev_height must be a positive integer")
        found = c.found.eq(True)
        paused = c.paused.eq(True)
        missing = c.missing.eq(True)
        if not (found.astype(int) + paused.astype(int) + missing.astype(int)).eq(1).all():
            raise ValueError("cohorts found/paused/missing must partition the original group")
        if (c.mid_obs.eq(True) & ~found).any():
            raise ValueError("cohorts mid_obs requires found")

    for target, source in FIELD_MAPPING.items():
        daily[target] = daily[source] if source in daily else np.nan
    egroups = e.groupby("trade_date", sort=False).indices
    cgroups = c.groupby("trade_date", sort=False).indices
    additions = []
    for _, row in daily.iterrows():
        day = row.trade_date
        x = e.iloc[egroups.get(day, [])]
        cohort = c.iloc[cgroups.get(day, [])]
        reasons = []
        expected_events = _expected(row, "market_observed_n", "amount_original_n")
        event_status = "complete" if len(x) else "missing"
        if len(x) and expected_events is not None and len(x) != expected_events:
            event_status = "partial"
        derived = {field: np.nan for field in ["mkt_advance_n", "mkt_advance_pct", *_HEIGHT_FIELDS]}
        if event_status == "complete":
            eligible = x.loc[x.eligible.eq(True)]
            expected_n = _expected(row, "N", "eligible_n")
            if expected_n is not None and len(eligible) != expected_n:
                event_status = "partial"
            else:
                close, reference = _cents(eligible, "events")
                advances = int(close.gt(reference).sum())
                derived["mkt_advance_n"] = advances
                derived["mkt_advance_pct"] = advances / len(eligible) if len(eligible) else np.nan
                up = eligible.loc[eligible.U.eq(True)]
                heights = pd.to_numeric(up.height, errors="coerce")
                if not (np.isfinite(heights) & heights.ge(1) & heights.eq(np.floor(heights))).all():
                    raise ValueError("events eligible U height must be a positive integer")
                for height in range(1, 6):
                    derived[f"mkt_height_{height}_n"] = int(heights.eq(height).sum())
                derived["mkt_height_6plus_n"] = int(heights.ge(6).sum())
        if event_status != "complete":
            reasons.append(f"events_{event_status}")

        expected_cohort = _expected(row, "cohort_original_n", "prev_cohort_original_n")
        if len(cohort):
            cohort_status = "complete" if expected_cohort is None or len(cohort) == expected_cohort else "partial"
        else:
            cohort_status = "empty" if expected_cohort == 0 else "missing"
        if cohort_status in ("complete", "empty"):
            for name, group in (("all", cohort), ("chain", cohort.loc[pd.to_numeric(cohort.prev_height, errors="coerce").ge(2)])):
                stats = _group_stats(group)
                derived.update({f"mkt_{name}_{quantity}": value for quantity, value in stats.items()})
        else:
            reasons.append(f"cohorts_{cohort_status}")
            for name in ("all", "chain"):
                derived.update({f"mkt_{name}_{quantity}": np.nan for quantity in _GROUP_QUANTITIES})
        for name in ("all", "chain"):
            if derived.get(f"mkt_{name}_paused_n", 0) > 0:
                reasons.append(f"{name}_paused")
            if derived.get(f"mkt_{name}_missing_n", 0) > 0:
                reasons.append(f"{name}_missing")
            if derived.get(f"mkt_{name}_unobservable_n", 0) > 0:
                reasons.append(f"{name}_unobservable")
        derived["mkt_event_input_status"] = event_status
        derived["mkt_cohort_input_status"] = cohort_status
        derived["mkt_adapter_quality"] = (
            "UNAVAILABLE" if event_status != "complete" and cohort_status not in ("complete", "empty")
            else "DEGRADED" if reasons else "OK"
        )
        derived["mkt_adapter_reasons"] = ";".join(reasons)
        additions.append(derived)
    extra_columns = ["mkt_advance_n", "mkt_advance_pct", *_HEIGHT_FIELDS]
    extra_columns += [f"mkt_{name}_{quantity}" for name in ("all", "chain") for quantity in _GROUP_QUANTITIES]
    extra_columns += ["mkt_event_input_status", "mkt_cohort_input_status", "mkt_adapter_quality", "mkt_adapter_reasons"]
    extra = pd.DataFrame(additions, index=daily.index, columns=extra_columns)
    unavailable_events = extra.mkt_event_input_status.ne("complete")
    if unavailable_events.any():
        daily.loc[unavailable_events, list(_EVENT_REUSED)] = np.nan
    return pd.concat([daily, extra], axis=1)


def load_formal(formal_dir: str | Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Read the explicitly selected F1 directory; never choose another version."""
    directory = Path(formal_dir)
    events = pd.read_parquet(directory / "events.parquet")
    cohorts = pd.read_parquet(directory / "cohorts.parquet")
    daily = pd.read_csv(directory / "sentiment_daily.csv", float_precision="round_trip")
    return events, cohorts, daily
