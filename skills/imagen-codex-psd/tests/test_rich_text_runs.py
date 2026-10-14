import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_stage3  # noqa: E402
import psd_writer  # noqa: E402
import stage3_writer_adapter  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class StyleRunContractTests(unittest.TestCase):
    def test_paragraph_ranges_cover_cr_terminated_paragraphs_in_utf16(self):
        editor_text = "WiFi😀\r\rDNS\r"
        lengths = stage3_writer_adapter._paragraph_run_lengths_utf16(editor_text)
        self.assertEqual(lengths, [7, 1, 4])
        self.assertEqual(sum(lengths), len(editor_text.encode("utf-16-be")) // 2)

        for invalid_text in ("WiFi😀\r\rDNS", "WiFi😀\n\r", "WiFi\r\rDNS\rx", ""):
            with self.subTest(invalid_text=invalid_text), self.assertRaisesRegex(
                    stage3_writer_adapter.WriterAdapterError, "Editor.Text"):
                stage3_writer_adapter._paragraph_run_lengths_utf16(invalid_text)

    def test_style_runs_cover_display_text_in_order_and_keep_utf16_units(self):
        text = "A😀\nB"
        style = {"style_runs": [
            {"text": "A", "font_weight": "Bold", "font_size_pt": 18, "color_rgba": [1, 2, 3]},
            {"text": "😀\nB", "font_weight": "Heavy", "font_size_pt": 24, "color_rgba": [4, 5, 6, 255]},
        ]}
        runs = prepare_stage3._validated_style_runs(style, text, "MIXED", 0)
        self.assertEqual("".join(run["text"] for run in runs), text)
        self.assertEqual(runs[0]["color_rgba"], [1, 2, 3, 255])

        for invalid_runs in (
            [style["style_runs"][0], {**style["style_runs"][1], "text": "😀\n"}],
            [style["style_runs"][0], {**style["style_runs"][1], "text": "😀\nBB"}],
            [{**style["style_runs"][0], "text": "😀"}, style["style_runs"][1]],
        ):
            with self.subTest(invalid_runs=invalid_runs), self.assertRaisesRegex(
                    prepare_stage3.Stage3InputError, "cover display text exactly once"):
                prepare_stage3._validated_style_runs({"style_runs": invalid_runs}, text, "MIXED", 0)

        editor_text = text.replace("\n", "\r") + "\r"
        lengths = [len(run["text"].replace("\n", "\r").encode("utf-16-be")) // 2
                   for run in style["style_runs"]]
        lengths[-1] += 1
        self.assertEqual(sum(lengths), len(editor_text.encode("utf-16-be")) // 2)
        self.assertEqual(lengths, [1, 5])

    def test_vertical_centering_is_rejected_until_native_reflow_is_verified(self):
        with self.assertRaisesRegex(prepare_stage3.Stage3InputError, "unsupported"):
            prepare_stage3._validated_vertical_alignment({"vertical_alignment": "center"}, "CENTERED")
        self.assertEqual(prepare_stage3._validated_vertical_alignment({"vertical_alignment": "top"}, "TOP"), "top")

    def test_style_scope_rejects_an_unselected_id_and_duplicate_parent_id(self):
        expected = {"SELECTED_A", "SELECTED_B"}
        with self.assertRaisesRegex(prepare_stage3.Stage3InputError, r"unexpected=\['UNSELECTED'\]"):
            prepare_stage3._style_index([
                {"text_item_id": "SELECTED_A"}, {"text_item_id": "UNSELECTED"},
            ], expected)
        with self.assertRaisesRegex(prepare_stage3.Stage3InputError, "duplicate text_item_id"):
            prepare_stage3._style_index([
                {"text_item_id": "SELECTED_A"}, {"text_item_id": "SELECTED_A"},
            ], expected)

    def test_mixed_and_legacy_text_reopen_with_top_layout_and_paragraph_ranges(self):
        from psd_tools import PSDImage

        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "synthetic-run"
            stage2 = run / "stage2"
            stage2.mkdir(parents=True)
            source = run / "source.png"
            base = stage2 / "clean-base.png"
            Image.new("RGB", (420, 180), "#ffffff").save(source)
            Image.new("RGB", (420, 180), "#f2f0eb").save(base)
            source_hash, base_hash = sha256(source), sha256(base)
            text = "WiFi😀\n\nDNS"
            items = [{
                "id": "MIXED_COPY", "text": text,
                "source_box_px": [10, 16, 395, 140], "layout_box_px": [22, 10, 360, 148],
                "font_weight": "Heavy", "font_size_pt": 30, "color_rgba": [20, 20, 20, 255],
                "style_runs": [
                    {"text": "Wi", "font_weight": "Heavy", "font_size_pt": 31, "color_rgba": [210, 40, 30, 255], "tracking": 0},
                    {"text": "Fi", "font_weight": "Normal", "font_size_pt": 19, "color_rgba": [20, 40, 180, 255], "tracking": 0},
                    {"text": "😀\n\n", "font_weight": "Bold", "font_size_pt": 19, "color_rgba": [25, 145, 50, 255], "tracking": 0},
                    {"text": "DNS", "font_weight": "Heavy", "font_size_pt": 24, "color_rgba": [225, 125, 15, 255], "tracking": 0},
                ],
                "alignment": "center", "vertical_alignment": "top", "tracking": 0,
                "auto_leading": False, "leading_pt": 38,
                "rotation_deg": -6.0, "rotation_anchor_px": [180.0, 78.0],
            }, {
                "id": "LEGACY_SINGLE_STYLE", "text": "legacy line one\nlegacy line two",
                "source_box_px": [14, 140, 270, 32], "layout_box_px": [22, 140, 300, 34],
                "font_weight": "Normal", "font_size_pt": 14, "color_rgba": [35, 45, 55, 255],
                "alignment": "left", "vertical_alignment": "top", "tracking": 0,
                "auto_leading": False, "leading_pt": 16, "rotation_deg": 0.0,
            }]
            page = {
                "schema_version": 1, "status": "ready_for_stage_3",
                "source": {"source_png_path": "../source.png", "source_png_sha256": source_hash,
                           "size_px": {"width": 420, "height": 180}},
                "background": {"status": "ready", "decision": "imported_clean_base",
                               "path": "clean-base.png", "sha256": base_hash},
                "foreground_assets": [], "text_inventory": items,
            }
            page_path = stage2 / "page_rebuild.json"
            write_json(page_path, page)
            output = run / "output"
            manifest = stage3_writer_adapter.build(page_path, output)

            psd = PSDImage.open(output / manifest["psd"])
            layers = [layer for layer in psd.descendants() if getattr(layer, "kind", None) != "group"]
            layer = next(layer for layer in layers if str(layer.name).rstrip("\x00") == "Text - MIXED_COPY")
            self.assertEqual(layer.kind, "type")
            self.assertEqual(str(layer.text).replace("\r", "\n"), text)

            tysh = layer._record.tagged_blocks.get_data(b"TySh")
            self.assertIsNotNone(tysh)
            self.assertAlmostEqual(float(tysh.transform[1]), math.sin(math.radians(-6.0)), places=5)
            self.assertEqual((tysh.left, tysh.top, tysh.right, tysh.bottom), (22, 10, 382, 158))

            paragraph = layer.engine_dict["ParagraphRun"]
            self.assertEqual(len(list(paragraph["RunArray"])), 3)
            self.assertEqual([float(run["ParagraphSheet"]["Properties"]["SpaceBefore"])
                              for run in paragraph["RunArray"]], [0.0, 0.0, 0.0])
            self.assertEqual([int(value) for value in paragraph["RunLengthArray"]], [7, 1, 4])

            style_run = layer.engine_dict["StyleRun"]
            entries = list(style_run["RunArray"])
            lengths = [int(value) for value in style_run["RunLengthArray"]]
            self.assertEqual(lengths, [2, 2, 4, 4])  # Includes emoji surrogate pair, hard breaks, and final CR.
            self.assertEqual(sum(lengths), len((text.replace("\n", "\r") + "\r").encode("utf-16-be")) // 2)
            font_set = list(layer.resource_dict["FontSet"])
            expected = [
                (31, [210, 40, 30, 255], "SourceHanSansCN-Heavy"),
                (19, [20, 40, 180, 255], "SourceHanSansCN-Normal"),
                (19, [25, 145, 50, 255], "SourceHanSansCN-Bold"),
                (24, [225, 125, 15, 255], "SourceHanSansCN-Heavy"),
            ]
            for entry, (size, color, font_name) in zip(entries, expected):
                data = entry["StyleSheet"]["StyleSheetData"]
                self.assertAlmostEqual(float(data["FontSize"]), size, places=2)
                self.assertFalse(bool(data["AutoLeading"]))
                self.assertAlmostEqual(float(data["Leading"]), 38, places=2)
                self.assertAlmostEqual(float(data["BaselineShift"]), 0, places=3)
                font_index = int(data["Font"])
                self.assertEqual(str(font_set[font_index]["Name"].value), font_name)
                values = [float(value) for value in data["FillColor"]["Values"]]
                rgba_engine = [color[3] / 255, color[0] / 255, color[1] / 255, color[2] / 255]
                for actual, wanted in zip(values, rgba_engine):
                    self.assertAlmostEqual(actual, wanted, places=3)

            cached = layer.topil().convert("RGBA")
            pixels = {cached.getpixel((x, y)) for y in range(cached.height) for x in range(cached.width)}
            for _, color, _ in expected:
                self.assertTrue(any(pixel[3] >= 240 and max(abs(pixel[channel] - color[channel])
                                                            for channel in range(3)) <= 2
                                    for pixel in pixels), f"cached preview lost style-run color {color}")
            reopened = next(item for item in manifest["reopen_validation"]["layers"]
                            if item["name"] == "Text - MIXED_COPY")
            self.assertEqual(len(reopened["style_runs"]), 4)
            self.assertTrue(all(item["baseline_shift_pt"] == 0 for item in reopened["style_runs"]))
            legacy = next(layer for layer in layers if str(layer.name).rstrip("\x00") == "Text - LEGACY_SINGLE_STYLE")
            self.assertEqual(legacy.kind, "type")
            self.assertEqual(str(legacy.text).replace("\r", "\n"), "legacy line one\nlegacy line two")
            legacy_paragraph = legacy.engine_dict["ParagraphRun"]
            self.assertEqual(len(list(legacy_paragraph["RunArray"])), 2)
            self.assertEqual([float(run["ParagraphSheet"]["Properties"]["SpaceBefore"])
                              for run in legacy_paragraph["RunArray"]], [0.0, 0.0])
            self.assertEqual([int(value) for value in legacy_paragraph["RunLengthArray"]], [16, 16])
            legacy_style = legacy.engine_dict["StyleRun"]
            self.assertEqual(len(list(legacy_style["RunArray"])), 1)
            self.assertEqual(manifest["layout_anchor_validation"]["status"], "STRUCTURAL_ONLY_PASS")
            self.assertEqual(manifest["layout_anchor_validation"]["photoshop_manual_check"],
                             "PENDING_MANUAL_PHOTOSHOP")
            mixed_anchor = manifest["layout_anchor_validation"]["items"]["MIXED_COPY"]
            self.assertEqual(mixed_anchor["layout_origin_px"], [22, 10])
            self.assertTrue(mixed_anchor["cache_and_tysh_share_layout_frame_center"])

    def test_rotated_text_outside_canvas_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "synthetic-overflow"
            stage2 = run / "stage2"
            stage2.mkdir(parents=True)
            source = run / "source.png"
            base = stage2 / "clean-base.png"
            Image.new("RGB", (120, 80), "#ffffff").save(source)
            Image.new("RGB", (120, 80), "#f2f0eb").save(base)
            text = "WIFI"
            page = {
                "schema_version": 1, "status": "ready_for_stage_3",
                "source": {"source_png_path": "../source.png", "source_png_sha256": sha256(source),
                           "size_px": {"width": 120, "height": 80}},
                "background": {"status": "ready", "decision": "imported_clean_base",
                               "path": "clean-base.png", "sha256": sha256(base)},
                "foreground_assets": [], "text_inventory": [{
                    "id": "EDGE_ROTATION", "text": text,
                    "source_box_px": [8, 8, 80, 30], "layout_box_px": [8, 8, 80, 30],
                    "font_weight": "Bold", "font_size_pt": 22, "color_rgba": [20, 20, 20, 255],
                    "alignment": "left", "vertical_alignment": "top", "tracking": 0,
                    "auto_leading": False, "leading_pt": 24, "rotation_deg": -45.0,
                    "rotation_anchor_px": [0.0, 79.0],
                }],
            }
            page_path = stage2 / "page_rebuild.json"
            write_json(page_path, page)
            with self.assertRaisesRegex(psd_writer.Stage3Error, "ROTATED_TEXT_OUTSIDE_CANVAS"):
                psd_writer.build_psd(page_path, run / "output")


if __name__ == "__main__":
    unittest.main()
