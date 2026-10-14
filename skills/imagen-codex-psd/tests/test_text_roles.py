import argparse
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ocr_first_adapter  # noqa: E402
import prepare_stage3  # noqa: E402
import selection_gate  # noqa: E402
import text_background  # noqa: E402
import text_first  # noqa: E402
import text_roles  # noqa: E402


class TextRoleWorkflowTests(unittest.TestCase):
    def make_run(self, root: Path):
        source_path = root / "poster.png"
        Image.new("RGB", (120, 100), (230, 230, 230)).save(source_path)
        run_dir = text_first.prepare_image(source_path, root / "runs")
        selection_gate.record_mode(run_dir, "partial")
        raw_path = run_dir / "ocr-first" / "ocr-candidates.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "schema_version": 1,
            "backend": "fixture",
            "advisory_only": True,
            "source": {"path": str(run_dir / "source.png"),
                       "sha256": text_first.sha256_file(run_dir / "source.png"),
                       "width_px": 120, "height_px": 100},
            "candidates": [
                {"id": "P01", "text_candidate": "正确", "box_px": [5, 5, 24, 12], "block_label": "text"},
                {"id": "P02", "text_candidate": "正确", "box_px": [48, 5, 24, 12], "block_label": "text"},
                {"id": "P03", "text_candidate": "建议", "box_px": [5, 30, 35, 12], "block_label": "text"},
            ],
        }
        raw_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        review_path = run_dir / "text-role-review.json"
        text_roles.create_review_template(run_dir, raw_path, review_path)
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["review_status"] = "reviewed"
        review["reviewer"] = "fixture visual reviewer"
        review["whole_source_review"] = {
            "status": "reviewed", "whole_source_checked": True,
            "ocr_regions_compared_to_source": True,
            "notes": "Compared every inventory entry with the complete source and context.",
        }
        review["accounted_raw_ocr_ids"] = ["P01", "P02", "P03"]
        for index, item in enumerate(review["items"]):
            item.update({
                "source_location": ["poster heading", "illustration label", "advice card"][index],
                "text_role": ["main_copy", "illustration_internal", "main_copy"][index],
                "native_representation": "editable" if index != 1 else None,
                "source_text_confirmed": True,
                "source_position_confirmed": True,
                "review_status": "reviewed",
                "review_reason": [
                    "Context and source position identify this occurrence as a reader-facing heading.",
                    "The same wording is inside an illustration and remains in the background.",
                    "This is a reader-facing recommendation in a separate advice card.",
                ][index],
            })
        review["items"].append({
            "id": "T004", "text": "隐藏陷阱：供电不稳", "source_bbox_px": [55, 30, 40, 12],
            "source_location": "purple panel heading", "text_role": "main_copy",
            "native_representation": "editable", "source_kind": "visual_supplement",
            "source_ocr_ids": [], "source_text_confirmed": True,
            "source_position_confirmed": True, "review_status": "reviewed",
            "review_reason": "Visible main-copy heading missed by OCR; manually transcribed and positioned from the source.",
        })
        review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        compiled_path = run_dir / "ocr-first" / "reviewed-candidates.json"
        decisions_path = run_dir / "decisions.json"
        text_roles.compile_review(run_dir, raw_path, review_path, compiled_path, decisions_path)
        return run_dir, raw_path, review_path, compiled_path, decisions_path

    def make_ready_plan(self, root: Path):
        run_dir, raw_path, review_path, candidates_path, decisions_path = self.make_run(root)
        map_one = selection_gate.build_number_map(run_dir, raw_path, review_path)
        self.assertEqual([item["ocr_id"] for item in map_one["items"]], ["T001", "T003", "T004"])
        self.assertIn("roles-", map_one["preview_path"])
        self.assertEqual(map_one["role_review_sha256"], text_first.sha256_file(review_path))
        selection_gate.record_method(run_dir, "numbers")
        selection_gate.record_numbers(run_dir, "1")
        coverage_path = root / "coverage.json"
        coverage = text_first.build_whole_image_review(run_dir, candidates_path, coverage_path)
        text_roles.bind_coverage(decisions_path, coverage_path,
                                 text_first.sha256_file(run_dir / "source.png"),
                                 text_first.sha256_file(candidates_path))
        plan = text_first.build_text_plan(run_dir, candidates_path, decisions_path,
                                          coverage_arg=coverage_path, require_ready=True)
        return run_dir, review_path, candidates_path, decisions_path, coverage_path, plan

    def background_review(self, plan: dict, retained: bool = True) -> dict:
        items = {item["id"]: item for item in plan["items"]}
        selected = sorted(plan["partial_selection"]["selected_ids"])
        preserved = sorted(plan["partial_selection"]["preserved_inventory_ids"])
        return {
            "schema_version": 1, "status": "accepted", "reviewer": "fixture background reviewer",
            "source_sha256": plan["source"]["coordinate_sha256"],
            "text_plan_sha256": "bound-in-test", "clean_base_sha256": "base-hash",
            "text_review": {
                "status": "accepted", "summary": "Reviewed source and candidate item by item.",
                "cleared_main_text_ids": selected,
                "preserved_internal_text_ids": preserved,
                "cleared_selection_items": [
                    {"id": item_id, "text": items[item_id]["text"],
                     "source_bbox_px": items[item_id]["source_bbox_px"],
                     "visible_in_source": True, "removed_from_clean_base": True}
                    for item_id in selected
                ],
                "preserved_selection_items": [
                    {"id": item_id, "text": items[item_id]["text"],
                     "source_bbox_px": items[item_id]["source_bbox_px"],
                     "visible_in_source": True, "retained_in_clean_base": retained,
                     "position_matches_source": True}
                    for item_id in preserved
                ],
                "illustration_text_identity": {
                    "checked": True, "identities_preserved": True,
                    "summary": "Every internal illustration label was compared with the source.",
                },
            },
            "nontext_structure_review": {
                "status": "accepted", "missing_from_clean_base": [],
                "summary": "Checked the complete canvas and its non-text structure.",
            },
        }

    def test_default_new_run_and_pending_or_incomplete_role_review_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "poster.png"
            Image.new("RGB", (60, 50), "white").save(source)
            run = text_first.prepare_image(source, root / "runs")
            manifest = json.loads((run / "manifest.json").read_text())
            self.assertEqual(manifest["workflow"]["version"], "2.1.1")
            with self.assertRaisesRegex(ocr_first_adapter.OCRAdapterError, "explicitly answers"):
                ocr_first_adapter.run_one_page(run / "source.png", run / "ocr-first")

            selection_gate.record_mode(run, "partial")
            raw = run / "ocr-first" / "ocr-candidates.json"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_text(json.dumps({"source": {"sha256": text_first.sha256_file(run / "source.png"),
                                                    "width_px": 60, "height_px": 50},
                                      "candidates": [{"id": "P01", "text_candidate": "标题",
                                                      "box_px": [5, 5, 25, 12]}]}, ensure_ascii=False))
            draft = run / "text-role-review.json"
            review_doc = text_roles.create_review_template(run, raw, draft)
            with self.assertRaisesRegex(selection_gate.SelectionError, "pending or lacks reviewer"):
                selection_gate.build_number_map(run, raw, draft)
            review_doc["review_status"] = "reviewed"
            review_doc["reviewer"] = "fixture reviewer"
            review_doc["whole_source_review"] = {
                "status": "reviewed", "whole_source_checked": True,
                "ocr_regions_compared_to_source": True, "notes": "Compared against the source.",
            }
            draft.write_text(json.dumps(review_doc, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(text_roles.TextRoleError, "account for every raw OCR ID"):
                text_roles.compile_review(run, raw, draft, run / "reviewed.json", run / "decisions.json")

    def test_corrected_split_and_manual_supplement_are_bound_without_ocr_resubmission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, raw, review, compiled, decisions = self.make_run(root)
            doc = json.loads(review.read_text())
            # One merged raw OCR item may support multiple visually corrected source regions.
            doc["items"][0]["source_kind"] = "ocr_correction"
            doc["items"][0]["source_ocr_ids"] = ["P01"]
            doc["items"][0]["text"] = "正确（标题）"
            doc["items"][0]["source_bbox_px"] = [4, 4, 28, 14]
            split = copy.deepcopy(doc["items"][0])
            split.update({"id": "T005", "text": "正确（示意标签）", "text_role": "illustration_internal",
                          "native_representation": None, "source_bbox_px": [48, 4, 26, 13],
                          "source_location": "split illustration label",
                          "review_reason": "The same merged OCR wording is assigned to a distinct illustration region.",
                          "source_ocr_ids": ["P01"]})
            doc["items"].append(split)
            review.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
            compiled_value, _ = text_roles.compile_review(run, raw, review, compiled, decisions)
            self.assertEqual(compiled_value["text_role_review"]["raw_ocr_sha256"], text_first.sha256_file(raw))
            self.assertEqual(len(compiled_value["candidates"]), 5)
            split_item = next(item for item in compiled_value["candidates"] if item["id"] == "T005")
            self.assertEqual(split_item["source_ocr_ids"], ["P01"])

    def test_only_main_copy_is_numbered_and_only_selected_main_copy_becomes_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, review, candidates, _decisions, _coverage, plan = self.make_ready_plan(root)
            self.assertEqual(plan["workflow"]["version"], "2.1.1")
            by_id = {item["id"]: item for item in plan["items"]}
            editable = {item_id for item_id, item in by_id.items() if item["classification"] == "editable"}
            internal_types = {item_id for item_id, item in by_id.items()
                              if item.get("text_role") == "illustration_internal" and item["classification"] == "editable"}
            self.assertEqual(editable, set(plan["partial_selection"]["selected_ids"]))
            self.assertEqual(editable, {"T001"})
            self.assertFalse(internal_types)
            self.assertIn("T004", plan["partial_selection"]["preserved_inventory_ids"])
            self.assertEqual(by_id["T004"]["text"], "隐藏陷阱：供电不稳")
            self.assertEqual(plan["partial_selection"]["role_review_sha256"], text_first.sha256_file(review))

    def test_ledger_hash_changes_make_a_new_non_overwriting_number_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, raw, review, _candidates, _decisions = self.make_run(root)
            first = selection_gate.build_number_map(run, raw, review)
            first_path = Path(first["preview_path"])
            first_hash = text_first.sha256_file(first_path)
            doc = json.loads(review.read_text())
            doc["items"][3]["review_reason"] = "The missed heading is located in the separate lower-right card."
            review.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(selection_gate.SelectionError, "review changed after numbering"):
                selection_gate.record_numbers(run, "1")
            text_roles.compile_review(run, raw, review, run / "ocr-first" / "reviewed-candidates.json",
                                      run / "decisions.json")
            second = selection_gate.build_number_map(run, raw, review)
            self.assertNotEqual(first["preview_path"], second["preview_path"])
            self.assertTrue(first_path.is_file())
            self.assertEqual(text_first.sha256_file(first_path), first_hash)
            self.assertIn(text_first.sha256_file(review)[:12], second["preview_path"])

    def test_screenshot_branch_can_select_one_reviewed_target_without_duplicate_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, raw, review, candidates, decisions = self.make_run(root)
            selection_gate.build_number_map(run, raw, review)
            selection_gate.record_method(run, "screenshot")
            screenshot = root / "target.png"
            Image.new("RGB", (24, 12), "white").save(screenshot)
            gate = selection_gate.add_screenshot_target(run, screenshot, "5,5,24,12", "正确")
            target = gate["screenshot_targets"][0]
            self.assertEqual(gate["status"], "ready")
            self.assertEqual(target["duplicate_of_inventory_id"], "T001")
            self.assertEqual(gate["selected_ids"], ["T001"])
            coverage_path = root / "coverage.png.json"
            coverage = text_first.build_whole_image_review(run, candidates, coverage_path)
            text_roles.bind_coverage(decisions, coverage_path,
                                     text_first.sha256_file(run / "source.png"),
                                     text_first.sha256_file(candidates))
            plan = text_first.build_text_plan(run, candidates, decisions,
                                              coverage_arg=coverage_path, require_ready=True)
            item_ids = [item["id"] for item in plan["items"]]
            self.assertEqual(len(item_ids), len(set(item_ids)))
            self.assertNotIn("M001", item_ids)
            self.assertEqual(plan["partial_selection"]["selected_ids"], ["T001"])

    def test_missing_or_false_retained_item_blocks_background_and_stage3_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, review_path, candidates, decisions, coverage, plan = self.make_ready_plan(root)
            accepted = self.background_review(plan)
            self.assertEqual(text_background._validate_accepted_review(accepted, plan["items"], plan), [])

            missing = copy.deepcopy(accepted)
            missing["text_review"]["preserved_selection_items"] = [
                row for row in missing["text_review"]["preserved_selection_items"] if row["id"] != "T004"]
            with self.assertRaisesRegex(text_background.BackgroundError, "preserved_selection_items"):
                text_background._validate_accepted_review(missing, plan["items"], plan)

            false_retained = copy.deepcopy(accepted)
            next(row for row in false_retained["text_review"]["preserved_selection_items"]
                 if row["id"] == "T004")["retained_in_clean_base"] = False
            with self.assertRaisesRegex(text_background.BackgroundError, "lacks visual confirmation"):
                text_background._validate_accepted_review(false_retained, plan["items"], plan)

            plan_path = run / "test-plan.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
            style_path = run / "empty-style.json"
            style_path.write_text("{}\n")
            bad_review_path = run / "false-retention-review.json"
            bad_review_path.write_text(json.dumps(false_retained, ensure_ascii=False, indent=2) + "\n")
            bad_background = {
                "source_sha256": text_first.sha256_file(run / "source.png"),
                "text_plan_sha256": text_first.sha256_file(plan_path),
                "background_ready": True, "ready_for_psd": False,
                "partial_selection": plan["partial_selection"],
                "text_role_review_sha256": plan["text_role_inventory"]["sha256"],
                "background_review": {"status": "accepted", "path": str(bad_review_path),
                                      "sha256": text_first.sha256_file(bad_review_path)},
            }
            background_path = run / "fake-ready-background.json"
            background_path.write_text(json.dumps(bad_background, ensure_ascii=False, indent=2) + "\n")
            args = argparse.Namespace(source=str(run / "source.png"), text_plan=str(plan_path),
                                      background_build=str(background_path), style_plan=str(style_path),
                                      title_reviews=[], out_dir=str(run / "stage3-output"))
            with self.assertRaisesRegex(prepare_stage3.Stage3InputError, "does not prove complete text retention"):
                prepare_stage3.prepare(args)


if __name__ == "__main__":
    unittest.main()
