"""Behavioral tests for exact text anchoring and conservative timestamp exports."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "subtitle_utils.py"
spec = importlib.util.spec_from_file_location("subtitle_utils", MODULE_PATH)
subtitles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subtitles)


def characters(text, start=0.0, step=0.2):
    return [{"text": char, "start": start + i * step, "end": start + (i + 1) * step} for i, char in enumerate(text)]


class SubtitleTests(unittest.TestCase):
    def codes(self, result):
        return {issue["code"] for issue in result["pending"]}

    def test_chinese_punctuation_and_semantic_sentence_cue_mapping(self):
        raw = "今天观察市场，注意成交量。明天再看。"
        units = characters("今天观察市场注意成交量明天再看")
        result = subtitles.prepare_subtitles(units, raw, 10, max_cue_chars=8)
        self.assertTrue(result["validation"]["passed"], result["pending"])
        self.assertEqual("".join(s["text"] for s in result["sentences"]), raw)
        self.assertEqual("".join(c["text"] for c in result["cues"]), raw)
        self.assertEqual(len(result["sentences"]), 2)
        self.assertGreater(len(result["sentences"][0]["cue_ids"]), 1)
        self.assertEqual(result["cues"][0]["text"], "今天观察市场，")
        for cue in result["cues"]:
            self.assertIn(cue["id"], result["sentences"][cue["sentence_ids"][0] - 1]["cue_ids"])

    def test_english_words_decimal_and_digits_are_atomic(self):
        raw = "AI ETF涨3.5%，2026年再评估。"
        units = [{"text": "AI", "start": 0, "end": .2}, {"text": "ETF", "start": .2, "end": .4}]
        units += characters("涨352026年再评估", start=.4)
        result = subtitles.prepare_subtitles(units, raw, 10, max_cue_chars=6)
        self.assertEqual("".join(c["text"] for c in result["cues"]), raw)
        self.assertTrue(result["validation"]["passed"], result["pending"])
        for atomic in ("AI", "ETF", "3.5%", "2026"):
            self.assertEqual(sum(atomic in cue["text"] for cue in result["cues"]), 1)
        self.assertEqual(len(result["sentences"]), 1)

    def test_missing_text_is_visible_and_not_fabricated(self):
        raw = "风险增加，注意仓位。"
        result = subtitles.prepare_subtitles(characters("风险注意仓位"), raw, 10)
        self.assertFalse(result["validation"]["passed"])
        self.assertIn("missing_alignment", self.codes(result))
        self.assertIn("增加", result["raw_text"])
        self.assertEqual(subtitles.render_srt(result), "")
        self.assertIn("增加", subtitles.render_markdown(result))
        self.assertLess(result["validation"]["alignment_coverage"], 1)

    def test_punctuation_maps_by_text_anchor_not_unit_character_count(self):
        units = [{"text": "风险很大", "start": 0, "end": 1}, {"text": "注意", "start": 1, "end": 2}]
        result = subtitles.prepare_subtitles(units, "风险大。注意。", 3)
        self.assertEqual([s["text"] for s in result["sentences"]], ["风险大。", "注意。"])
        self.assertIn("unit_text_unmatched", self.codes(result))
        self.assertEqual(result["sentences"][1]["start"], 1)
        self.assertEqual(result["validation"]["exported_cue_count"], 1)

    def test_zero_length_character_retained_with_pending(self):
        units = [{"text": "风", "start": 1, "end": 1}, {"text": "险", "start": 1, "end": 1.2}]
        result = subtitles.prepare_subtitles(units, "风险。", 3)
        self.assertEqual(result["units"][0]["start"], 1)
        self.assertEqual(result["units"][0]["end"], 1)
        self.assertEqual(result["cues"][0]["text"], "风险。")
        self.assertIn("zero_duration", self.codes(result))
        self.assertFalse(result["validation"]["passed"])
        self.assertFalse(result["cues"][0]["exportable"])

    def test_non_monotonic_times_are_preserved(self):
        units = [{"text": "甲", "start": 2, "end": 2.2}, {"text": "乙", "start": 1, "end": 1.2}]
        result = subtitles.prepare_subtitles(units, "甲乙。", 3)
        self.assertEqual(result["units"], units)
        self.assertEqual(result["cues"][0]["start"], 2)
        self.assertEqual(result["cues"][0]["end"], 1.2)
        self.assertIn("non_monotonic", self.codes(result))
        self.assertEqual(subtitles.render_srt(result), "")

    def test_explicit_aggregate_zero_duration_uses_native_outer_boundaries(self):
        units = [{"text": "风", "start": 1, "end": 1.08}, {"text": "险", "start": 1.08, "end": 1.08}, {"text": "大", "start": 1.08, "end": 1.24}]
        strict = subtitles.prepare_subtitles(units, "风险大。", 3)
        aggregate = subtitles.prepare_subtitles(units, "风险大。", 3, aggregate_zero_duration=True)
        self.assertEqual(subtitles.render_srt(strict), "")
        self.assertEqual(aggregate["units"], units)
        cue = aggregate["cues"][0]
        self.assertEqual((cue["start"], cue["end"]), (1, 1.24))
        self.assertEqual(cue["zero_duration_unit_indices"], [1])
        self.assertEqual(cue["timing_basis"], "aggregate_native_unit_boundaries")
        self.assertFalse(cue["strict_timing_valid"])
        self.assertTrue(cue["exportable"])
        self.assertFalse(aggregate["validation"]["passed"])
        self.assertIn("zero_duration", self.codes(aggregate))
        self.assertIn("风险大。", subtitles.render_srt(aggregate))

    def test_aggregate_keeps_isolated_zero_and_other_anomalies_withheld(self):
        cases = [
            [{"text": "甲", "start": 1, "end": 1}],
            [{"text": "甲", "start": 1, "end": 1}, {"text": "乙", "start": 2, "end": 2}],
            [{"text": "甲", "start": 1, "end": 1}, {"text": "乙", "start": .5, "end": .8}],
            [{"text": "甲", "start": 1, "end": 1}, {"text": "乙", "start": 1, "end": 4}],
        ]
        for units in cases:
            with self.subTest(units=units):
                raw = "".join(unit["text"] for unit in units)
                result = subtitles.prepare_subtitles(units, raw, 3, aggregate_zero_duration=True)
                self.assertEqual(subtitles.render_srt(result), "")
                self.assertFalse(result["validation"]["passed"])

    def test_real_1yJDFSJ_9lw_cue_duplicate_zero_points_aggregate_only(self):
        # Frozen actual boundary values from cue 138; no audio or model loading.
        text = "我认为是一个大概率事件啊，我认为是个大概率事件啊。"
        frozen = [
            ('我', 616.0017499999999, 616.16175), ('认', 616.16175, 616.32175),
            ('为', 616.32175, 616.40175), ('是', 616.40175, 616.56175),
            ('一', 616.56175, 616.56175), ('个', 616.56175, 616.64175),
            ('大', 616.7217499999999, 616.88175), ('概', 616.88175, 617.04175),
            ('率', 617.04175, 617.12175), ('事', 617.12175, 617.28175),
            ('件', 617.36175, 617.52175), ('啊', 617.6017499999999, 617.76175),
            ('我', 617.8417499999999, 617.92175), ('认', 617.92175, 618.0017499999999),
            ('为', 618.0017499999999, 618.0017499999999), ('是', 618.0017499999999, 618.0017499999999),
            ('个', 618.0817499999999, 618.16175), ('大', 618.16175, 618.2417499999999),
            ('概', 618.2417499999999, 618.40175), ('率', 618.40175, 618.4817499999999),
            ('事', 618.4817499999999, 618.64175), ('件', 618.64175, 618.80175),
            ('啊', 618.88175, 618.9617499999999),
        ]
        units = [{"text": char, "start": start, "end": end} for char, start, end in frozen]
        strict = subtitles.prepare_subtitles(units, text, 700)
        aggregate = subtitles.prepare_subtitles(units, text, 700, aggregate_zero_duration=True)
        self.assertEqual(subtitles.render_srt(strict), "")
        self.assertEqual(aggregate["units"], units)
        self.assertEqual(aggregate["cues"][0]["start"], 616.0017499999999)
        self.assertEqual(aggregate["cues"][0]["end"], 618.9617499999999)
        self.assertEqual(aggregate["cues"][0]["timing_basis"], "aggregate_native_unit_boundaries")
        self.assertIn("duplicate_boundary", self.codes(aggregate))
        self.assertIn("zero_duration", self.codes(aggregate))
        self.assertFalse(aggregate["validation"]["passed"])
        self.assertIn(text, subtitles.render_srt(aggregate))

    def test_adjacent_three_zero_points_keep_warnings_and_positive_native_bounds(self):
        units = [{"text": "甲", "start": 1, "end": 1}, {"text": "乙", "start": 1, "end": 1},
                 {"text": "丙", "start": 1, "end": 1}, {"text": "丁", "start": 1, "end": 1.16}]
        result = subtitles.prepare_subtitles(units, "甲乙丙丁", 3, aggregate_zero_duration=True)
        self.assertTrue(result["cues"][0]["exportable"])
        self.assertEqual(result["units"], units)
        self.assertEqual(sum(i["code"] == "duplicate_boundary" for i in result["pending"]), 2)
        self.assertFalse(result["validation"]["passed"])

    def test_positive_duplicate_overlap_and_all_zero_duplicate_remain_withheld(self):
        cases = [
            [{"text": "甲", "start": 1, "end": 1.16}, {"text": "乙", "start": 1, "end": 1.16}],
            [{"text": "甲", "start": 1, "end": 1.16}, {"text": "乙", "start": 1.08, "end": 1.24}],
            [{"text": "甲", "start": 1, "end": 1}, {"text": "乙", "start": 1, "end": 1}],
        ]
        for units in cases:
            with self.subTest(units=units):
                result = subtitles.prepare_subtitles(units, "甲乙", 3, aggregate_zero_duration=True)
                self.assertEqual(subtitles.render_srt(result), "")
                self.assertEqual(result["units"], units)
                self.assertFalse(result["validation"]["passed"])

    def test_short_zero_fragment_prefers_next_and_retains_pre_merge_provenance(self):
        units = [{"text": "嗯", "start": 1, "end": 1, "chunk_index": 0},
                 {"text": "市场", "start": 1.2, "end": 2, "chunk_index": 0}]
        baseline = subtitles.prepare_subtitles(units, "嗯。市场。", 3, aggregate_zero_duration=True)
        result = subtitles.prepare_subtitles(units, "嗯。市场。", 3, aggregate_zero_duration=True, merge_short_fragments=True)
        self.assertEqual(result["pre_merge_cues"], baseline["cues"])
        self.assertEqual(result["pre_merge_pending"], baseline["pending"])
        self.assertEqual(result["units"], units)
        self.assertEqual(result["raw_text"], "嗯。市场。")
        self.assertEqual(len(result["cues"]), 1)
        cue = result["cues"][0]
        self.assertEqual((cue["text"], cue["start"], cue["end"]), ("嗯。市场。", 1, 2))
        self.assertEqual(cue["original_cue_ids"], [1, 2])
        self.assertEqual(cue["sentence_ids"], [1, 2])
        self.assertEqual([s["cue_ids"] for s in result["sentences"]], [[1], [1]])
        self.assertEqual(result["short_fragment_merges"][0]["direction"], "next")
        self.assertFalse(result["validation"]["passed"])
        self.assertIn("zero_duration", self.codes(result))

    def test_short_tail_of_same_sentence_prefers_previous(self):
        units = [{"text": "市场", "start": 0, "end": 1, "chunk_index": 0},
                 {"text": "嗯", "start": 1, "end": 1, "chunk_index": 0},
                 {"text": "观察", "start": 1.5, "end": 2, "chunk_index": 0}]
        result = subtitles.prepare_subtitles(units, "市场嗯。观察。", 3, aggregate_zero_duration=True,
                                             max_cue_chars=2, merge_short_fragments=True)
        self.assertEqual([c["text"] for c in result["cues"]], ["市场嗯。", "观察。"])
        self.assertEqual(result["short_fragment_merges"][0]["direction"], "previous")
        self.assertEqual(result["cues"][0]["end"], 1)

    def test_short_merge_rejects_far_pause_chunk_boundary_and_bad_time(self):
        cases = [
            [{"text": "好", "start": 1, "end": 1, "chunk_index": 0}, {"text": "市场", "start": 3.01, "end": 4, "chunk_index": 0}],
            [{"text": "好", "start": 1, "end": 1, "chunk_index": 0}, {"text": "市场", "start": 1.2, "end": 2, "chunk_index": 1}],
            [{"text": "好", "start": 1, "end": 1, "chunk_index": 0}, {"text": "市场", "start": .5, "end": 2, "chunk_index": 0}],
            [{"text": "好", "start": 1, "end": 1}, {"text": "市场", "start": 1.2, "end": 2}],
            [{"text": "好", "start": -1, "end": -1, "chunk_index": 0}, {"text": "市场", "start": 1.2, "end": 2, "chunk_index": 0}],
        ]
        for units in cases:
            with self.subTest(units=units):
                result = subtitles.prepare_subtitles(units, "好。市场。", 5, aggregate_zero_duration=True, merge_short_fragments=True)
                self.assertEqual(result["short_fragment_merges"], [])
                self.assertEqual(result["units"], units)
                self.assertEqual(len(result["cues"]), 2)

    def test_short_merge_cannot_cross_or_absorb_suspicious_span(self):
        units = [{"text": "好", "start": 1, "end": 1, "chunk_index": 0},
                 {"text": "市场", "start": 1.2, "end": 2, "chunk_index": 0}]
        for span in [[0, 1], [1, 2], [2, 4]]:
            with self.subTest(span=span):
                result = subtitles.prepare_subtitles(units, "好。市场。", 3, aggregate_zero_duration=True,
                    merge_short_fragments=True, blocked_raw_spans=[{"raw_span": span, "reason": "suspected_prompt_echo"}])
                self.assertEqual(result["short_fragment_merges"], [])
                self.assertEqual(result["blocked_raw_spans"][0]["raw_span"], span)

    def test_short_fragment_lexical_limit_and_missing_alignment_are_conservative(self):
        text = "一二三四五六七。市场。"
        units = [{"text": "一二三四五六七", "start": 1, "end": 1, "chunk_index": 0},
                 {"text": "市场", "start": 1.2, "end": 2, "chunk_index": 0}]
        result = subtitles.prepare_subtitles(units, text, 3, merge_short_fragments=True)
        self.assertEqual(result["short_fragment_merges"], [])
        configured = subtitles.prepare_subtitles(units, text, 3, merge_short_fragments=True, short_fragment_max_lexical=7)
        self.assertEqual(len(configured["short_fragment_merges"]), 1)
        missing = subtitles.prepare_subtitles([units[1]], "好。市场。", 3, merge_short_fragments=True)
        self.assertEqual(missing["short_fragment_merges"], [])

    def test_chained_short_zero_fragments_preserve_order_and_never_interpolate(self):
        units = [{"text": "嗯", "start": 1, "end": 1, "chunk_index": 0},
                 {"text": "OK", "start": 1.1, "end": 1.1, "chunk_index": 0},
                 {"text": "市场", "start": 1.2, "end": 2, "chunk_index": 0}]
        result = subtitles.prepare_subtitles(units, "嗯。OK。市场。", 3, aggregate_zero_duration=True, merge_short_fragments=True)
        self.assertEqual(result["cues"][0]["text"], "嗯。OK。市场。")
        self.assertEqual((result["cues"][0]["start"], result["cues"][0]["end"]), (1, 2))
        self.assertEqual(result["cues"][0]["original_cue_ids"], [1, 2, 3])
        self.assertEqual(result["units"], units)

    def test_merge_flag_false_leaves_aggregated_outputs_identical(self):
        units = [{"text": "嗯", "start": 1, "end": 1, "chunk_index": 0},
                 {"text": "市场", "start": 1.2, "end": 2, "chunk_index": 0}]
        default = subtitles.prepare_subtitles(units, "嗯。市场。", 3, aggregate_zero_duration=True)
        explicit = subtitles.prepare_subtitles(units, "嗯。市场。", 3, aggregate_zero_duration=True, merge_short_fragments=False)
        self.assertEqual(default, explicit)
        self.assertNotIn("pre_merge_cues", default)
        self.assertEqual(subtitles.render_srt(default), subtitles.render_srt(explicit))

    def test_valid_oral_interjections_merge_with_native_times_but_financial_short_phrases_do_not(self):
        for phrase in ["嗯", "嗯嗯", "啊", "哦", "呃", "好", "好的", "好吧", "OK", "ok", "okay"]:
            with self.subTest(phrase=phrase):
                units = [{"text": phrase, "start": 0, "end": 1, "chunk_index": 0},
                         {"text": "观察市场", "start": 1.5, "end": 3, "chunk_index": 0}]
                result = subtitles.prepare_subtitles(units, phrase + "。观察市场。", 4, merge_short_fragments=True)
                self.assertEqual(len(result["cues"]), 1)
                self.assertEqual(result["cues"][0]["text"], phrase + "。观察市场。")
                self.assertEqual((result["cues"][0]["start"], result["cues"][0]["end"]), (0, 3))
                self.assertEqual(result["units"], units)
                self.assertEqual(result["short_fragment_merges"][0]["fragment_kind"], "oral_interjection")
                self.assertEqual(result["short_fragment_merges"][0]["direction"], "next")
                self.assertTrue(result["cues"][0]["strict_timing_valid"])
                self.assertFalse(result["validation"]["passed"])
        for phrase in ["买入", "卖出", "五块", "市场", "好股票"]:
            with self.subTest(phrase=phrase):
                units = [{"text": phrase, "start": 0, "end": .5, "chunk_index": 0},
                         {"text": "观察市場", "start": .6, "end": 3, "chunk_index": 0}]
                result = subtitles.prepare_subtitles(units, phrase + "。观察市場。", 4, merge_short_fragments=True)
                self.assertEqual(result["short_fragment_merges"], [])
                self.assertEqual(len(result["cues"]), 2)

    def test_valid_oral_fragment_limits_and_blocked_span_cannot_be_bypassed(self):
        for duration, gap, chunk, blocked in [(1.001, .1, 0, []), (.5, .601, 0, []),
                                             (.5, .1, 1, []), (.5, .1, 0, [[2, 4]])]:
            with self.subTest(duration=duration, gap=gap, chunk=chunk, blocked=blocked):
                units = [{"text": "好", "start": 0, "end": duration, "chunk_index": 0},
                         {"text": "市场", "start": duration + gap, "end": 3, "chunk_index": chunk}]
                result = subtitles.prepare_subtitles(units, "好。市场。", 4, merge_short_fragments=True, blocked_raw_spans=blocked)
                self.assertEqual(result["short_fragment_merges"], [])
                self.assertEqual(result["units"], units)

    def test_merge_settings_reject_invalid_limits_and_spans(self):
        for kwargs in [{"short_fragment_max_gap": -1}, {"oral_fragment_max_gap": float("nan")},
                       {"oral_fragment_max_duration": -1}, {"short_fragment_max_lexical": True},
                       {"blocked_raw_spans": [[0, 99]]}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                subtitles.prepare_subtitles([], "好", 1, merge_short_fragments=True, **kwargs)

    def test_all_source_validation_cases(self):
        units = [
            {"text": "负", "start": -1, "end": 0},
            {"text": "反", "start": 2, "end": 1},
            {"text": "长", "start": 3, "end": 50},
            {"text": "重", "start": 3, "end": 50},
            {"text": "", "start": 51, "end": 52},
            {"text": "无", "start": None, "end": 53},
        ]
        validation = subtitles.validate_units(units, 10)
        codes = {issue["code"] for issue in validation["issues"]}
        self.assertTrue({"negative_time", "reversed_time", "exceeds_duration", "excessive_span", "duplicate_boundary", "empty_text", "invalid_time"} <= codes)
        self.assertFalse(validation["passed"])

    def test_pause_splits_without_interpolating(self):
        units = [{"text": "先看", "start": 0, "end": .5}, {"text": "再等", "start": 1.5, "end": 2}]
        result = subtitles.prepare_subtitles(units, "先看，再等", 3)
        self.assertTrue(result["validation"]["passed"], result["pending"])
        self.assertEqual([s["text"] for s in result["sentences"]], ["先看，", "再等"])
        self.assertEqual([c["start"] for c in result["cues"]], [0, 1.5])

    def test_coarse_unit_crosses_sentences_without_duplicate_fake_times(self):
        units = [{"text": "先看市场。注意风险。", "start": 0, "end": 2}]
        result = subtitles.prepare_subtitles(units, units[0]["text"], 3, max_cue_chars=20)
        self.assertEqual(len(result["sentences"]), 2)
        self.assertEqual(len(result["cues"]), 1)
        self.assertEqual(result["cues"][0]["sentence_ids"], [1, 2])
        self.assertIsNone(result["sentences"][0]["start"])
        self.assertIn("sentence_boundary_inside_unit", self.codes(result))
        self.assertTrue(result["cues"][0]["exportable"])

    def test_long_unit_kept_whole_and_display_issue_reported(self):
        text = "不能人为拆分这一个词的时间"
        result = subtitles.prepare_subtitles([{"text": text, "start": 0, "end": 2}], text, 3, max_cue_chars=4)
        self.assertEqual(len(result["cues"]), 1)
        self.assertEqual(result["cues"][0]["text"], text)
        self.assertIn("cue_too_long", self.codes(result))

    def test_repetitive_digits_do_not_trigger_sequence_matcher_autojunk(self):
        text = "一" * 210 + "二"
        result = subtitles.prepare_subtitles(characters(text, step=.01), text, 5)
        self.assertEqual(result["validation"]["alignment_coverage"], 1)
        self.assertTrue(result["validation"]["passed"], result["pending"])

    def test_exact_normalized_lexical_content_avoids_sequence_matcher(self):
        # Actual NFKC/case-fold anchors, punctuation, coarse words and extra
        # metadata must survive the fast path, including pending zero times.
        raw = "AI均线，涨3.5%。风险大。"
        units = [
            {"text": "ＡＩ", "start": 0, "end": .2, "confidence": .9},
            {"text": "均线", "start": .2, "end": .6},
            {"text": "涨", "start": .6, "end": .8},
            {"text": "3.5", "start": .8, "end": 1.1},
            {"text": "风险", "start": 1.1, "end": 1.4},
            {"text": "大", "start": 1.4, "end": 1.4},
        ]
        with patch.object(subtitles, "SequenceMatcher", side_effect=AssertionError("Exact content must not invoke SequenceMatcher")):
            result = subtitles.prepare_subtitles(units, raw, 3, aggregate_zero_duration=True)
        self.assertEqual(result["units"], units)
        self.assertEqual(result["validation"]["alignment_coverage"], 1)
        self.assertEqual("".join(cue["text"] for cue in result["cues"]), raw)
        self.assertIn("zero_duration", self.codes(result))
        self.assertEqual(result["cues"][0]["unit_indices"], [0, 1, 2, 3])
        self.assertEqual((result["cues"][0]["start"], result["cues"][0]["end"]), (0, 1.1))

    def test_long_repetitive_chinese_exact_anchors_skip_global_matcher(self):
        # A long repeated financial transcript used to drive a quadratic
        # SequenceMatcher call. Verify the costly call is absent rather than
        # impose a machine-dependent stopwatch threshold.
        paragraph = "观察均线，注意成交量。"
        repetitions = 4000
        raw = paragraph * repetitions
        lexical = "观察均线注意成交量"
        units = [{"text": lexical, "start": i * 2, "end": i * 2 + 1}
                 for i in range(repetitions)]
        with patch.object(subtitles, "SequenceMatcher", side_effect=AssertionError("Long exact content must use direct anchors")):
            alignment = subtitles._alignment(units, raw)
        self.assertEqual(alignment["coverage"], 1)
        self.assertEqual(alignment["issues"], [])
        self.assertEqual(alignment["raw_to_unit"][0], 0)
        self.assertEqual(alignment["raw_to_unit"][len(raw) - 2], repetitions - 1)
        self.assertEqual(len(alignment["unit_spans"]), repetitions)

    def test_mismatched_text_keeps_sequence_matcher_fallback(self):
        original_matcher = subtitles.SequenceMatcher
        with patch.object(subtitles, "SequenceMatcher", wraps=original_matcher) as matcher:
            result = subtitles.prepare_subtitles(characters("风险注意仓位"), "风险增加，注意仓位。", 10)
        matcher.assert_called_once()
        self.assertEqual(matcher.call_args.kwargs, {"autojunk": False})
        self.assertIn("missing_alignment", self.codes(result))
        self.assertLess(result["validation"]["alignment_coverage"], 1)

    def test_unknown_duration_and_submillisecond_time_do_not_pass(self):
        units = [{"text": "甲", "start": 0, "end": .0001}]
        unknown = subtitles.prepare_subtitles(units, "甲", None)
        self.assertIn("duration_unchecked", self.codes(unknown))
        self.assertFalse(unknown["validation"]["passed"])
        tiny = subtitles.prepare_subtitles(units, "甲", 1)
        self.assertIn("timestamp_resolution_collision", self.codes(tiny))
        self.assertEqual(subtitles.render_srt(tiny), "")

    def test_pause_cannot_split_a_numeric_string(self):
        units = [{"text": "20", "start": 0, "end": .5}, {"text": "26", "start": 1.5, "end": 2}]
        result = subtitles.prepare_subtitles(units, "2026", 3)
        self.assertEqual([s["text"] for s in result["sentences"]], ["2026"])
        self.assertTrue(result["validation"]["passed"], result["pending"])

    def test_invalid_non_finite_times_remain_pending_and_json_explicit(self):
        units = [{"text": "甲", "start": float("nan"), "end": float("inf")}]
        result = subtitles.prepare_subtitles(units, "甲", 1)
        self.assertIn("invalid_time", self.codes(result))
        self.assertFalse(result["validation"]["passed"])
        with tempfile.TemporaryDirectory() as directory:
            paths = subtitles.write_subtitle_outputs(result, directory)
            recovered = json.loads(Path(paths["json"]).read_text())
            self.assertEqual(recovered["units"][0]["start"], {"_non_finite_number": "nan"})
            self.assertEqual(recovered["units"][0]["end"], {"_non_finite_number": "inf"})
            self.assertEqual(Path(paths["srt"]).read_text(), "")

    def test_non_mapping_unit_is_validation_failure(self):
        result = subtitles.prepare_subtitles(["invalid"], "甲", 1)
        self.assertIn("invalid_unit", self.codes(result))
        self.assertFalse(result["validation"]["passed"])

    def test_format_exports_and_complete_json_round_trip(self):
        units = [{"text": "市场", "start": 3601.125, "end": 3602.5, "confidence": .9}]
        result = subtitles.prepare_subtitles(units, "市场。", 4000)
        with tempfile.TemporaryDirectory() as directory:
            paths = subtitles.write_subtitle_outputs(result, directory, "sample")
            self.assertEqual(set(paths), {"json", "srt", "vtt", "markdown"})
            recovered = json.loads(Path(paths["json"]).read_text())
            self.assertEqual(recovered, result)
            self.assertTrue(Path(paths["srt"]).read_text().startswith("1\n01:00:01,125 --> 01:00:02,500\n市场。"))
            self.assertTrue(Path(paths["vtt"]).read_text().startswith("WEBVTT\n\n01:00:01.125"))
            self.assertIn("通过", Path(paths["markdown"]).read_text())


if __name__ == "__main__":
    unittest.main()
