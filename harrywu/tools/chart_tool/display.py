"""Display-only cropping for canonical chart views."""
from __future__ import annotations

from copy import deepcopy

from .contracts import ChartError, ChartView, parse_date


_ZOOM_WARNING = "DISPLAY_ZOOM_WITHIN_CUTOFF"


def crop_dates(view: ChartView, start: str | None, end: str | None) -> ChartView:
    """Crop an existing view by inclusive trade-date bounds.

    This mirrors ``cli.zoom_view`` and never loads or creates bars.
    """
    if start is None and end is None:
        return view
    view.validate()
    if start is not None:
        parse_date(start)
    if end is not None:
        parse_date(end)
    if start is not None and end is not None and start > end:
        raise ChartError("INVALID_REQUEST", "Zoom start must not exceed zoom end")

    selected = view.bars.copy()
    if start is not None:
        selected = selected.loc[selected.trade_date >= start]
    if end is not None:
        selected = selected.loc[selected.trade_date <= end]
    metadata = deepcopy(view.metadata)
    metadata["warnings"] = list(dict.fromkeys(metadata["warnings"] + [_ZOOM_WARNING]))
    metadata["incomplete"] = bool(selected.is_partial.any()) if "is_partial" in selected.columns else False
    if selected.empty:
        metadata["gap_dates"] = []
    else:
        first, last = selected.trade_date.iloc[0], selected.trade_date.iloc[-1]
        metadata["gap_dates"] = [day for day in metadata["gap_dates"] if first <= day <= last]
    result = ChartView(view.ts_code, view.period, selected.reset_index(drop=True), metadata)
    result.validate()
    return result


def crop_last_bars(view: ChartView, n: int) -> ChartView:
    """Keep the last ``n`` already-visible bars, without crossing the cutoff."""
    if type(n) is not int or n <= 0:
        raise ChartError("INVALID_REQUEST", "The displayed bar count must be a positive integer")
    if n >= len(view.bars):
        return view
    view.validate()
    selected = view.bars.iloc[-n:].copy()
    metadata = deepcopy(view.metadata)
    metadata["warnings"] = list(dict.fromkeys(metadata["warnings"] + [_ZOOM_WARNING]))
    metadata["incomplete"] = bool(selected.is_partial.any()) if "is_partial" in selected.columns else False
    first, last = selected.trade_date.iloc[0], selected.trade_date.iloc[-1]
    metadata["gap_dates"] = [day for day in metadata["gap_dates"] if first <= day <= last]
    result = ChartView(view.ts_code, view.period, selected.reset_index(drop=True), metadata)
    result.validate()
    return result


def crop_sessions(view: ChartView, n: int) -> ChartView:
    """Keep the last ``n`` visible sessions from a minute view.

    Session boundaries come from the view's existing ``trade_date`` values;
    this function never infers missing bars or reads another source.
    """
    if type(n) is not int or n <= 0:
        raise ChartError("INVALID_REQUEST", "The displayed session count must be a positive integer")
    if view.period == "daily":
        raise ChartError("INVALID_REQUEST", "Session cropping only supports minute views")
    view.validate()
    if view.bars.empty:
        return view
    sessions = list(dict.fromkeys(str(day) for day in view.bars["trade_date"].tolist()))
    if n >= len(sessions):
        return view
    kept = set(sessions[-n:])
    selected = view.bars.loc[view.bars["trade_date"].astype(str).isin(kept)].copy()
    metadata = deepcopy(view.metadata)
    metadata["warnings"] = list(dict.fromkeys(metadata["warnings"] + [_ZOOM_WARNING]))
    metadata["incomplete"] = bool(selected.is_partial.any()) if "is_partial" in selected.columns else False
    first, last = str(selected.trade_date.iloc[0]), str(selected.trade_date.iloc[-1])
    metadata["gap_dates"] = [day for day in metadata["gap_dates"] if first <= day <= last]
    result = ChartView(view.ts_code, view.period, selected.reset_index(drop=True), metadata)
    result.validate()
    return result
