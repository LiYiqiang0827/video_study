from dataclasses import replace
import pytest
from chart_tool.contracts import ChartError, RenderOptions


def test_amendment3_render_options_and_invalid_shapes():
    base = RenderOptions(price_scale="log", annotations=({"kind": "marker", "at": "2026-01-08", "price": 12.0, "text": "参考", "style": "reference"},))
    base.validate()
    for change in ({"price_scale": "bad"}, {"auto_width": 1}, {"min_slot_px": True}, {"min_slot_px": float("nan")}, {"min_slot_px": 21}, {"max_width_px": 639}, {"annotations": []}, {"annotations": ({"kind": "vline"},)}, {"annotations": ({"kind": "vline", "at": None, "price": None, "text": "test", "style": "note"},)}):
        with pytest.raises(ChartError) as exc:
            replace(base, **change).validate()
        assert exc.value.code == "INVALID_REQUEST"
