import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import text_first  # noqa: E402
import selection_gate  # noqa: E402


class WholeImageCoverageContractTests(unittest.TestCase):
    def test_human_review_is_source_bound_and_detector_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "poster.png"
            Image.new("RGB", (64, 48), "white").save(source_path)
            run_dir = text_first.prepare_image(source_path, root / "runs")
            manifest_path = run_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["workflow"]["version"] = "2.1.0"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            gate_path = selection_gate.gate_path(run_dir)
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            gate["workflow_version"] = "2.1.0"
            gate_path.write_text(json.dumps(gate, ensure_ascii=False), encoding="utf-8")
            selection_gate.record_mode(run_dir, "full")
            source_hash = text_first.sha256_file(run_dir / "source.png")
            ocr_path = root / "ocr.json"
            ocr = {"backend": "manual", "advisory_only": False,
                   "source": {"sha256": source_hash, "width_px": 64, "height_px": 48},
                   "candidates": [{"id": "P01", "text_candidate": "标题", "box_px": [8, 6, 30, 12]}]}
            ocr_path.write_text(json.dumps(ocr, ensure_ascii=False), encoding="utf-8")
            coverage_path = root / "coverage.json"
            coverage = text_first.build_whole_image_review(run_dir, ocr_path, coverage_path)
            self.assertEqual(coverage["method"], "whole_image_visual_review")
            self.assertEqual(coverage["regions"], [{"id": "V001", "source_bbox_px": [0, 0, 64, 48],
                                                     "scope": "entire coordinate image; human visual review only"}])
            self.assertNotIn("detector_report_path", coverage)
            pending = text_first.build_text_plan(run_dir, ocr_path, coverage_arg=coverage_path)
            self.assertFalse(pending["review"]["ready_for_stage2"])
            self.assertEqual(pending["visual_coverage"]["method"], "whole_image_visual_review")

            decisions = {
                "source_sha256": source_hash,
                "ocr_sha256": text_first.sha256_file(ocr_path),
                "reviewer": "reviewer",
                "assignments": [
                    {"ids": ["P01", "S01"], "classification": "editable",
                     "reason": "Read and classified against the full source.", "reviewer": "reviewer"}
                ],
                "supplements": [{"id": "S01", "text": "漏检补录", "source_bbox_px": [8, 24, 28, 10],
                                 "paragraph_group_id": "visual-supplement", "coverage_region_ids": ["V001"]}],
                "whole_image_visual_review": {
                    "status": "confirmed", "reviewer": "reviewer",
                    "reason": "Inspected original and overlay across the entire image.",
                    "coverage_artifact_sha256": text_first.sha256_file(coverage_path),
                    "whole_source_visual_review": True, "reviewed_region_ids": ["V001"],
                    "region_review_batches": [{"region_ids": ["V001"], "outcome": "checked_against_source",
                                               "reason": "Reviewed all visible regions."}],
                },
                "ocr_coverage_review": {"status": "confirmed", "reason": "OCR text checked visually."},
            }
            decisions_path = root / "decisions.json"
            decisions_path.write_text(json.dumps(decisions, ensure_ascii=False), encoding="utf-8")
            ready = text_first.build_text_plan(run_dir, ocr_path, decisions_path,
                                              coverage_arg=coverage_path, require_ready=True)
            self.assertTrue(ready["review"]["ready_for_stage2"])
            self.assertFalse(ready["ocr"]["advisory_only"])
            self.assertEqual(ready["ocr"]["manual_supplement_count"], 1)
            self.assertEqual(ready["items"][-1]["classification"], "editable")

    def test_visual_coverage_rejects_stale_source_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "poster.png"
            Image.new("RGB", (20, 16), "white").save(source_path)
            run_dir = text_first.prepare_image(source_path, root / "runs")
            manifest_path = run_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["workflow"]["version"] = "2.1.0"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            gate_path = selection_gate.gate_path(run_dir)
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            gate["workflow_version"] = "2.1.0"
            gate_path.write_text(json.dumps(gate, ensure_ascii=False), encoding="utf-8")
            selection_gate.record_mode(run_dir, "full")
            ocr_path = root / "ocr.json"
            ocr_path.write_text(json.dumps({"source": {"sha256": "0" * 64, "width_px": 20,
                                                        "height_px": 16}, "candidates": []}), encoding="utf-8")
            with self.assertRaisesRegex(text_first.PlanError, "another coordinate image"):
                text_first.build_whole_image_review(run_dir, ocr_path, root / "coverage.json")


if __name__ == "__main__":
    unittest.main()
