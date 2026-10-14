import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import ocr_first_adapter  # noqa: E402
import selection_gate  # noqa: E402
import text_background  # noqa: E402
import text_first  # noqa: E402
import text_roles  # noqa: E402


class SelectionGateTests(unittest.TestCase):
    def make_legacy_210(self, run_dir: Path):
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["workflow"]["version"] = "2.1.0"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        gate_path = selection_gate.gate_path(run_dir)
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        gate["workflow_version"] = "2.1.0"
        gate_path.write_text(json.dumps(gate, ensure_ascii=False), encoding="utf-8")

    def make_run(self, root: Path):
        source = root / "poster.png"
        Image.new("RGB", (120, 100), (245, 245, 245)).save(source)
        run_dir = text_first.prepare_image(source, root / "runs")
        self.make_legacy_210(run_dir)
        source_hash = text_first.sha256_file(run_dir / "source.png")
        ocr_dir = run_dir / "ocr-first"
        ocr_dir.mkdir()
        ocr_path = ocr_dir / "ocr-candidates.json"
        candidates = [
            {"id": "P09", "text_candidate": "gamma", "box_px": [60, 60, 30, 16]},
            {"id": "P06", "text_candidate": "beta", "box_px": [60, 20, 30, 16]},
            {"id": "P01", "text_candidate": "alpha", "box_px": [8, 20, 30, 16]},
        ]
        ocr = {"schema_version": 1, "backend": "fixture", "advisory_only": True,
               "source": {"path": str(run_dir / "source.png"), "sha256": source_hash,
                          "width_px": 120, "height_px": 100}, "candidates": candidates}
        ocr_path.write_text(json.dumps(ocr, ensure_ascii=False), encoding="utf-8")
        return run_dir, ocr_path, source_hash

    def test_pending_mode_blocks_ocr_and_downstream_even_if_artifact_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, ocr_path, source_hash = self.make_run(Path(tmp))
            with self.assertRaisesRegex(ocr_first_adapter.OCRAdapterError, "explicitly answers"):
                ocr_first_adapter.run_one_page(run_dir / "source.png", run_dir / "ocr-first")
            self.assertFalse((run_dir / "ocr-first" / "ocr-job.json").exists())
            with self.assertRaisesRegex(text_first.PlanError, "still pending"):
                text_first.build_whole_image_review(run_dir, ocr_path, Path(tmp) / "coverage.json")
            gate = selection_gate._read(selection_gate.gate_path(run_dir))
            self.assertEqual(gate["status"], "pending_mode")
            self.assertEqual(gate["source_sha256"], source_hash)

    def test_number_map_is_stable_hash_bound_and_bad_answers_never_mean_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, ocr_path, source_hash = self.make_run(Path(tmp))
            selection_gate.record_mode(run_dir, "partial")
            mapping = selection_gate.build_number_map(run_dir, ocr_path)
            self.assertEqual([(x["number"], x["ocr_id"]) for x in mapping["items"]],
                             [(1, "P01"), (2, "P06"), (3, "P09")])
            self.assertEqual(mapping["source_size_px"], [120, 100])
            self.assertEqual(mapping["preview_size_px"], [240, 200])
            for answer in ("", "1,x", "4", "1,1", "0", "１"):
                with self.subTest(answer=answer), self.assertRaises(selection_gate.SelectionError):
                    selection_gate.record_numbers(run_dir, answer)
            self.assertEqual(selection_gate.load_gate(run_dir)["status"], "pending_partial_method")

            ocr = json.loads(ocr_path.read_text(encoding="utf-8"))
            ocr["candidates"][0]["text_candidate"] = "gamma updated"
            ocr_path.write_text(json.dumps(ocr, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(selection_gate.SelectionError, "changed after the numbered overlay"):
                selection_gate.record_numbers(run_dir, "1,3")
            new_mapping = selection_gate.build_number_map(run_dir, ocr_path)
            self.assertNotEqual(mapping["ocr_sha256"], new_mapping["ocr_sha256"])
            gate = selection_gate.record_numbers(run_dir, "3，1")
            self.assertEqual(gate["selected_ids"], ["P09", "P01"])
            self.assertEqual(gate["selected_numbers"], [3, 1])
            self.assertEqual(gate["status"], "ready")
            self.assertEqual(gate["source_sha256"], source_hash)

    def test_partial_plan_keeps_all_copy_but_only_selected_items_editable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, ocr_path, source_hash = self.make_run(Path(tmp))
            selection_gate.record_mode(run_dir, "partial")
            selection_gate.build_number_map(run_dir, ocr_path)
            selection_gate.record_numbers(run_dir, "1,3")
            coverage_path = Path(tmp) / "coverage.json"
            coverage = text_first.build_whole_image_review(run_dir, ocr_path, coverage_path)
            decisions = {
                "source_sha256": source_hash,
                "ocr_sha256": text_first.sha256_file(ocr_path),
                "ocr_coverage_review": {"status": "confirmed", "reason": "Fixture OCR checked."},
                "whole_image_visual_review": {
                    "status": "confirmed", "reviewer": "fixture reviewer",
                    "reason": "Synthetic source inspected as a complete canvas.",
                    "coverage_artifact_sha256": text_first.sha256_file(coverage_path),
                    "whole_source_visual_review": True, "reviewed_region_ids": ["V001"],
                    "region_review_batches": [{"region_ids": ["V001"],
                                               "outcome": "checked_against_source",
                                               "reason": "All fixture regions checked."}],
                },
            }
            decisions_path = Path(tmp) / "decisions.json"
            decisions_path.write_text(json.dumps(decisions), encoding="utf-8")
            plan = text_first.build_text_plan(run_dir, ocr_path, decisions_path,
                                              coverage_arg=coverage_path, require_ready=True)
            by_id = {item["id"]: item for item in plan["items"]}
            self.assertEqual({key for key, item in by_id.items() if item["classification"] == "editable"},
                             {"P01", "P09"})
            self.assertEqual({key for key, item in by_id.items() if item["classification"] == "background_internal"},
                             {"P06"})
            self.assertEqual(by_id["P06"]["text"], "beta")
            self.assertEqual(by_id["P06"]["source_bbox_px"], [60, 20, 30, 16])
            self.assertEqual(plan["partial_selection"]["preserved_ocr_ids"], ["P06"])
            self.assertEqual(plan["workflow"]["mode"], "partial")

            review = {"reviewer": "fixture reviewer", "text_review": {
                "status": "accepted", "cleared_main_text_ids": ["P01", "P09"],
                "preserved_internal_text_ids": ["P06"], "summary": "Synthetic scope check.",
                "cleared_selection_items": [
                    {"id": by_id[item_id]["id"], "text": by_id[item_id]["text"],
                     "source_bbox_px": by_id[item_id]["source_bbox_px"],
                     "visible_in_source": True, "removed_from_clean_base": True}
                    for item_id in ("P01", "P09")],
                "preserved_selection_items": [
                    {"id": "P06", "text": "beta", "source_bbox_px": [60, 20, 30, 16],
                     "visible_in_source": True, "retained_in_clean_base": True,
                     "position_matches_source": True}],
                "illustration_text_identity": {"checked": True, "identities_preserved": True,
                                                "summary": "Synthetic fixture has no illustration text."},
            }, "nontext_structure_review": {"status": "accepted", "missing_from_clean_base": [],
                                             "summary": "Synthetic fixture structure checked."}}
            self.assertEqual(text_background._validate_accepted_review(review, plan["items"], plan), [])
            review["text_review"]["preserved_selection_items"] = []
            with self.assertRaisesRegex(text_background.BackgroundError, "preserved_selection_items"):
                text_background._validate_accepted_review(review, plan["items"], plan)

    def test_screenshot_branch_requires_mapping_confirmation_and_hash_bound_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir, ocr_path, _ = self.make_run(root)
            selection_gate.record_mode(run_dir, "partial")
            selection_gate.build_number_map(run_dir, ocr_path)
            selection_gate.record_method(run_dir, "screenshot")
            screenshot = root / "attachment.png"
            Image.new("RGB", (16, 12), "white").save(screenshot)
            gate = selection_gate.add_screenshot_target(run_dir, screenshot, None, None, ambiguous=True)
            self.assertEqual(gate["status"], "pending_location_confirmation")
            with self.assertRaisesRegex(selection_gate.SelectionError, "still pending"):
                selection_gate.require_ready(run_dir)
            gate = selection_gate.confirm_target(run_dir, "M001", "96,84,16,12", "未识别文字", "user")
            self.assertEqual(gate["status"], "ready")
            self.assertEqual(gate["selected_ids"], ["M001"])
            evidence = Path(gate["screenshot_targets"][0]["evidence_path"])
            self.assertTrue(evidence.is_file())
            selection_gate.require_ready(run_dir)
            evidence.write_bytes(b"changed")
            with self.assertRaisesRegex(selection_gate.SelectionError, "missing or changed"):
                selection_gate.require_ready(run_dir)

    def test_overlapping_screenshot_target_stays_pending_until_user_resolves_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir, ocr_path, _ = self.make_run(root)
            selection_gate.record_mode(run_dir, "partial")
            selection_gate.build_number_map(run_dir, ocr_path)
            selection_gate.record_method(run_dir, "screenshot")
            screenshot = root / "overlap.png"
            Image.new("RGB", (12, 12), "white").save(screenshot)
            gate = selection_gate.add_screenshot_target(run_dir, screenshot, "10,22,12,10", "missed text")
            self.assertEqual(gate["status"], "pending_overlap_confirmation")
            self.assertEqual(gate["screenshot_targets"][0]["overlap_candidate_ids"], ["P01"])
            with self.assertRaisesRegex(selection_gate.SelectionError, "still pending"):
                selection_gate.require_ready(run_dir)
            with self.assertRaisesRegex(selection_gate.SelectionError, "explicitly confirm"):
                selection_gate.confirm_target(run_dir, "M001", "10,22,12,10", "missed text", "user")
            gate = selection_gate.confirm_target(run_dir, "M001", "10,22,12,10", "missed text", "user",
                                                confirm_overlap_distinct=True)
            self.assertEqual(gate["status"], "ready")
            self.assertEqual(gate["screenshot_targets"][0]["overlap_resolution"],
                             "user_confirmed_target_is_distinct_from_preserved_ocr_text")
            selection_gate.require_ready(run_dir)

    def test_legacy_201_manifest_remains_full_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _, _ = self.make_run(Path(tmp))
            manifest_path = run_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("workflow")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            selection_gate.gate_path(run_dir).unlink()
            gate = selection_gate.require_ready(run_dir)
            self.assertEqual(gate["mode"], "full")
            self.assertTrue(gate["legacy_2_0_1_full_mode"])


if __name__ == "__main__":
    unittest.main()
