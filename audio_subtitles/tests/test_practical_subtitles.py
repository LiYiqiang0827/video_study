import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import practical_subtitles as practical
from subtitle_utils import prepare_subtitles


def prepared(units, text, duration=30, chunks=None):
    output = prepare_subtitles(units, text, duration, aggregate_zero_duration=True)
    output["audio"] = {"source_offset_seconds": 100, "duration_seconds": duration}
    output["backend_metadata"] = {"chunks": chunks or []}
    return output


class PracticalSubtitleTests(unittest.TestCase):
    def test_long_pause_zero_fragment_uses_neighbor_interval_without_extending_it(self):
        units = [{"text": "市场", "start": 1, "end": 2, "chunk_index": 0},
                 {"text": "嗯", "start": 10, "end": 10, "chunk_index": 0},
                 {"text": "注意风险", "start": 20, "end": 21, "chunk_index": 0}]
        source = prepared(units, "市场。嗯。注意风险。")
        snapshot = copy.deepcopy(source)
        result = practical.generate_practical_subtitles(source)
        self.assertEqual(source, snapshot)
        self.assertEqual(result["units"], units)
        self.assertEqual(result["raw_text"], source["raw_text"])
        self.assertEqual(result["state"], "exported_auto")
        cue = result["cues"][1]
        self.assertEqual(cue["text"], "嗯。注意风险。")
        self.assertEqual((cue["start"], cue["end"]), (20, 21))
        self.assertEqual((cue["source_start"], cue["source_end"]), (120, 121))
        self.assertEqual(cue["timing_basis"], "approximate_neighbor_grouping")
        self.assertEqual(cue["source_cue_ids"], [2, 3])
        self.assertTrue(result["auto_summary"]["raw_mapping_complete"])
        self.assertEqual(result["source_cues"], source["cues"])
        self.assertEqual(result["source_pending"], source["pending"])
        self.assertEqual(result["auto_summary"]["unresolved_cue_count"], 0)

    def test_grammatical_tails_return_left_while_subjects_and_market_state_prefixes_go_right(self):
        for fragment, previous, direction in [("来。", "只要石油反弹，它肯定会起", "previous"),
                                               ("了。", "一口气涨停", "previous"),
                                               ("了吗？", "南企没有买，南企买", "previous"),
                                               ("破", "无所谓啊。", "next"),
                                               ("它", "核心就看，", "next"),
                                               ("我", "虚惊一场。", "next")]:
            with self.subTest(fragment=fragment):
                units = [{"text": previous, "start": 1, "end": 2, "chunk_index": 0},
                         {"text": fragment, "start": 10, "end": 10, "chunk_index": 0},
                         {"text": "了涨停，保持原识别", "start": 20, "end": 22, "chunk_index": 0}]
                text = previous + fragment + "了涨停，保持原识别。"
                result = practical.generate_practical_subtitles(prepared(units, text))
                action = next(a for a in result["auto_actions"] if a["action"] == "approximate_neighbor_grouping")
                self.assertEqual(action["direction"], direction)
                self.assertEqual(result["display_text"], text)
                self.assertEqual(result["units"], units)

    def test_adjacent_zero_fragments_keep_all_words_and_source_ids(self):
        units = [{"text": "起", "start": 1, "end": 2, "chunk_index": 0},
                 {"text": "来", "start": 8, "end": 8, "chunk_index": 0},
                 {"text": "哎", "start": 10, "end": 10, "chunk_index": 0},
                 {"text": "湖南黄金", "start": 20, "end": 22, "chunk_index": 0}]
        result = practical.generate_practical_subtitles(prepared(units, "起来。哎，湖南黄金。"))
        self.assertEqual(result["display_text"], "起来。哎，湖南黄金。")
        self.assertEqual(result["auto_summary"]["unresolved_cue_count"], 0)
        self.assertEqual([i for c in result["cues"] for i in c["source_cue_ids"]], [1, 2, 3, 4])

    def test_known_prompt_filter_is_explicit_and_keeps_financial_source_evidence(self):
        text = "市场。均线MA20。风险。"
        units = [{"text": "市场", "start": 1, "end": 2}, {"text": "均线MA20", "start": 3, "end": 3},
                 {"text": "风险", "start": 4, "end": 5}]
        result = practical.generate_practical_subtitles(prepared(units, text), blocked_raw_spans=[
            {"raw_span": [3, 10], "reason": "suspected_prompt_echo_requires_listening", "text": "均线MA20。"}])
        self.assertEqual(result["display_text"], "市场。风险。")
        self.assertEqual(result["raw_text"], text)
        self.assertEqual(result["units"], units)
        self.assertTrue(result["auto_summary"]["raw_mapping_complete"])
        self.assertNotIn("MA20", practical.render_srt(result))
        self.assertEqual(result["removed_raw_spans"][0]["reason"], "suspected_prompt_echo")

    def test_unknown_block_or_exact_text_mismatch_cannot_remove_financial_text(self):
        text = "均线MA20。风险。"
        units = [{"text": "均线MA20", "start": 1, "end": 2}, {"text": "风险", "start": 3, "end": 4}]
        for block in [{"raw_span": [0, 7], "reason": "unknown_financial_term"},
                      {"raw_span": [0, 7], "reason": "suspected_prompt_echo", "text": "错误文字"}]:
            with self.subTest(block=block):
                result = practical.generate_practical_subtitles(prepared(units, text), blocked_raw_spans=[block])
                self.assertEqual(result["display_text"], text)
                self.assertEqual(result["auto_summary"]["omitted_text_chars"], 0)
                self.assertTrue(result["auto_warnings"])

    def test_long_nonlexical_repetition_filters_but_repeated_financial_characters_do_not(self):
        text = "哼" * 8 + "。" + "涨" * 8 + "。五五五五五五五五。"
        units = [{"text": "哼" * 8, "start": 1, "end": 2}, {"text": "涨" * 8, "start": 3, "end": 4},
                 {"text": "五" * 8, "start": 5, "end": 6}]
        result = practical.generate_practical_subtitles(prepared(units, text))
        self.assertEqual(result["display_text"], "涨" * 8 + "。五五五五五五五五。")
        self.assertEqual(result["auto_summary"]["omitted_text_chars"], 9)
        self.assertEqual(result["raw_text"], text)

    def test_standalone_zero_chunk_uses_bounded_approximation_and_preserves_units(self):
        units = [{"text": "嗯", "start": 1, "end": 1, "chunk_index": 0},
                 {"text": "好", "start": 2, "end": 2, "chunk_index": 0}]
        result = practical.generate_practical_subtitles(prepared(units, "嗯。好。", 10,
                                                                [{"index": 0, "start_seconds": 0, "end_seconds": 10}]))
        self.assertEqual(result["auto_summary"]["estimated_cue_count"], 2)
        self.assertEqual(result["auto_summary"]["unresolved_cue_count"], 0)
        self.assertEqual(result["units"], units)
        self.assertLessEqual(result["cues"][0]["end"], result["cues"][1]["start"])
        self.assertTrue(all(c["timing_basis"] == "approximate_chunk_gap" for c in result["cues"]))

    def test_unknown_or_insufficient_chunk_time_returns_structured_unresolved_text(self):
        units = [{"text": "买入", "start": .2, "end": .2, "chunk_index": 0}]
        for chunks in [[], [{"index": 0, "start": 0, "end": .5}]]:
            result = practical.generate_practical_subtitles(prepared(units, "买入。", 1, chunks))
            self.assertEqual(result["display_text"], "买入。")
            self.assertEqual(result["auto_summary"]["unresolved_cue_count"], 1)
            self.assertEqual(practical.render_srt(result), "")
            self.assertEqual(result["auto_warnings"][-1]["code"], "unresolved_display_timing")
            self.assertFalse(result["validation"]["source_timing_verified"])

    def test_missing_alignment_cannot_be_given_a_false_model_time(self):
        source = prepared([{"text": "风险", "start": 1, "end": 2}], "买入。风险。")
        result = practical.generate_practical_subtitles(source)
        self.assertEqual(result["display_text"], "买入。风险。")
        self.assertGreater(result["auto_summary"]["unresolved_cue_count"], 0)
        self.assertIn("买入", result["unresolved_cues"][0]["text"])

    def test_millisecond_files_stay_inside_native_bounds(self):
        result = practical.generate_practical_subtitles(prepared([
            {"text": "市场", "start": .0004, "end": 1.0004}], "市场。", 1.0004))
        cue = result["cues"][0]
        self.assertEqual((cue["start"], cue["end"]), (.0004, 1.0004))
        self.assertEqual((cue["start_ms"], cue["end_ms"]), (1, 1000))
        self.assertIn("00:00:00,001 --> 00:00:01,000", practical.render_srt(result))

    def test_formats_and_nonfinite_evidence_have_explicit_json_representation(self):
        source = prepared([{"text": "买入", "start": float("nan"), "end": 2}], "买入。")
        result = practical.generate_practical_subtitles(source)
        with tempfile.TemporaryDirectory() as directory:
            paths = practical.write_practical_outputs(result, directory)
            self.assertEqual(set(paths), {"json", "srt", "vtt", "markdown"})
            stored = json.loads(Path(paths["json"]).read_text())
            self.assertEqual(stored["units"][0]["start"], {"_non_finite_number": "nan"})
            self.assertEqual(stored["state"], "exported_auto")
            self.assertTrue(Path(paths["vtt"]).read_text().startswith("WEBVTT"))
            self.assertNotIn("待验收", Path(paths["markdown"]).read_text())

    def test_invalid_caller_spans_and_path_stems_fail_clearly(self):
        source = prepared([{"text": "好", "start": 0, "end": 1}], "好。")
        with self.assertRaises(ValueError):
            practical.generate_practical_subtitles(source, blocked_raw_spans=[{"raw_span": [0, 999], "reason": "long_repetition"}])
        with self.assertRaises(ValueError):
            practical.write_practical_outputs(practical.generate_practical_subtitles(source), "/tmp", "../escape")


if __name__ == "__main__":
    unittest.main()
