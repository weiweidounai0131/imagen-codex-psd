#!/usr/bin/env python3
"""Create and validate a source-bound visual inventory of every poster text item."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


class TextRoleError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TextRoleError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TextRoleError(f"Expected a JSON object in {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _source_context(run_dir: Path, raw_ocr_path: Path) -> tuple[dict[str, Any], dict[str, Any], Path, str, str]:
    manifest = read_json(run_dir / "manifest.json")
    source = manifest.get("source", {})
    source_path = Path(source.get("coordinate_path", ""))
    if not source_path.is_absolute():
        source_path = run_dir / source_path
    if not source_path.is_file():
        raise TextRoleError("Prepared source image is missing.")
    source_hash = sha256_file(source_path)
    if source_hash != source.get("coordinate_sha256"):
        raise TextRoleError("Prepared source image hash changed.")
    ocr = read_json(raw_ocr_path)
    raw_hash = sha256_file(raw_ocr_path)
    ocr_source = ocr.get("source", {})
    if ocr_source.get("sha256") != source_hash:
        raise TextRoleError("OCR artifact belongs to another source image.")
    width, height = source.get("width_px"), source.get("height_px")
    if (ocr_source.get("width_px"), ocr_source.get("height_px")) != (width, height):
        raise TextRoleError("OCR dimensions do not match the prepared source image.")
    if not isinstance(ocr.get("candidates"), list):
        raise TextRoleError("OCR artifact has no candidates array.")
    return manifest, ocr, source_path, source_hash, raw_hash


def create_review_template(run_dir_arg: str | Path, raw_ocr_arg: str | Path,
                           output_arg: str | Path) -> dict[str, Any]:
    run_dir = Path(run_dir_arg).expanduser().resolve()
    raw_path = Path(raw_ocr_arg).expanduser().resolve()
    manifest, ocr, _source_path, source_hash, raw_hash = _source_context(run_dir, raw_path)
    from selection_gate import require_ocr_authorized
    require_ocr_authorized(run_dir, source_hash)
    source = manifest["source"]
    width, height = source["width_px"], source["height_px"]
    items = []
    raw_ids: set[str] = set()
    for index, candidate in enumerate(ocr["candidates"], 1):
        candidate_id = candidate.get("id") if isinstance(candidate, dict) else None
        text = candidate.get("text_candidate") if isinstance(candidate, dict) else None
        box = candidate.get("box_px") if isinstance(candidate, dict) else None
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in raw_ids:
            raise TextRoleError("Every raw OCR candidate needs a unique, non-empty ID.")
        if (not isinstance(text, str) or not text.strip() or not isinstance(box, list) or len(box) != 4
                or any(type(value) is not int for value in box)):
            raise TextRoleError(f"{candidate_id}: OCR text or source box is invalid.")
        x, y, w, h = box
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise TextRoleError(f"{candidate_id}: OCR source box is outside the source image.")
        raw_ids.add(candidate_id)
        items.append({
            "id": f"T{index:03d}",
            "text": text,
            "source_bbox_px": box,
            "source_location": "",
            "text_role": "pending",
            "native_representation": None,
            "source_kind": "ocr",
            "source_ocr_ids": [candidate_id],
            "source_text_confirmed": False,
            "source_position_confirmed": False,
            "review_status": "pending",
            "review_reason": "",
        })
    result = {
        "schema_version": 1,
        "task": "source_text_role_review",
        "workflow_version": "2.1.1",
        "source_sha256": source_hash,
        "raw_ocr_sha256": raw_hash,
        "raw_ocr_path": str(raw_path),
        "source_size_px": {"width": width, "height": height},
        "review_status": "pending",
        "reviewer": "",
        "whole_source_review": {
            "status": "pending",
            "whole_source_checked": False,
            "ocr_regions_compared_to_source": False,
            "notes": "",
        },
        "accounted_raw_ocr_ids": [],
        "items": items,
        "review_limits": "This record binds an explicit visual review and its completeness claim; software cannot prove that every visible text pixel was noticed.",
    }
    output = Path(output_arg).expanduser().resolve()
    write_json(output, result)
    return result


def validate_review(review: dict[str, Any], *, source_hash: str, raw_ocr_hash: str,
                    raw_ocr: dict[str, Any], width: int, height: int) -> list[dict[str, Any]]:
    if review.get("schema_version") != 1 or review.get("workflow_version") != "2.1.1":
        raise TextRoleError("Text role review has an unsupported schema or workflow version.")
    if review.get("task") != "source_text_role_review":
        raise TextRoleError("Review file is not a source_text_role_review.")
    if review.get("source_sha256") != source_hash or review.get("raw_ocr_sha256") != raw_ocr_hash:
        raise TextRoleError("Text role review belongs to another source image or raw OCR artifact.")
    if review.get("source_size_px") != {"width": width, "height": height}:
        raise TextRoleError("Text role review source dimensions changed.")
    reviewer = review.get("reviewer")
    whole = review.get("whole_source_review")
    if (review.get("review_status") != "reviewed" or not isinstance(reviewer, str) or not reviewer.strip()
            or not isinstance(whole, dict) or whole.get("status") != "reviewed"
            or whole.get("whole_source_checked") is not True
            or whole.get("ocr_regions_compared_to_source") is not True
            or not isinstance(whole.get("notes"), str) or not whole["notes"].strip()):
        raise TextRoleError("Whole-source text-role review is pending or lacks reviewer/evidence notes.")
    raw_candidates = raw_ocr.get("candidates")
    raw_ids = {row.get("id") for row in raw_candidates if isinstance(row, dict)}
    if len(raw_ids) != len(raw_candidates) or not all(isinstance(value, str) and value for value in raw_ids):
        raise TextRoleError("Raw OCR candidate IDs are invalid or duplicated.")
    accounted = review.get("accounted_raw_ocr_ids")
    if (not isinstance(accounted, list) or len(accounted) != len(set(accounted))
            or set(accounted) != raw_ids):
        raise TextRoleError("Review must account for every raw OCR ID exactly once in accounted_raw_ocr_ids.")
    items = review.get("items")
    if not isinstance(items, list) or not items:
        raise TextRoleError("Text role review has no reviewed inventory items.")
    item_ids: set[str] = set()
    accounted_by_items: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise TextRoleError("Each text role inventory entry must be an object.")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", item_id) or item_id in item_ids:
            raise TextRoleError("Inventory item IDs must be unique and use letters, numbers, _ or -.")
        item_ids.add(item_id)
        text, box = item.get("text"), item.get("source_bbox_px")
        if not isinstance(text, str) or not text.strip():
            raise TextRoleError(f"{item_id}: visible source text is missing.")
        if not isinstance(item.get("source_location"), str) or not item["source_location"].strip():
            raise TextRoleError(f"{item_id}: describe where this text appears in the source image.")
        if (not isinstance(box, list) or len(box) != 4 or any(type(value) is not int for value in box)):
            raise TextRoleError(f"{item_id}: source_bbox_px must be integer [x,y,width,height].")
        x, y, w, h = box
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise TextRoleError(f"{item_id}: source box is outside the source image.")
        role = item.get("text_role")
        if role not in {"main_copy", "illustration_internal"}:
            raise TextRoleError(f"{item_id}: role must be main_copy or illustration_internal; pending roles block numbering.")
        source_kind = item.get("source_kind")
        if source_kind not in {"ocr", "ocr_correction", "visual_supplement"}:
            raise TextRoleError(f"{item_id}: source_kind is invalid.")
        source_ids = item.get("source_ocr_ids")
        if (not isinstance(source_ids, list) or len(source_ids) != len(set(source_ids))
                or any(not isinstance(value, str) or value not in raw_ids for value in source_ids)):
            raise TextRoleError(f"{item_id}: source_ocr_ids must reference the bound raw OCR artifact.")
        if source_kind == "visual_supplement" and source_ids:
            raise TextRoleError(f"{item_id}: visual supplements have no OCR source IDs.")
        if source_kind != "visual_supplement" and not source_ids:
            raise TextRoleError(f"{item_id}: OCR corrections must identify their raw OCR source IDs.")
        accounted_by_items.update(source_ids)
        if (item.get("source_text_confirmed") is not True
                or item.get("source_position_confirmed") is not True
                or item.get("review_status") != "reviewed"):
            raise TextRoleError(f"{item_id}: exact wording and source position must be visually confirmed.")
        reason = item.get("review_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise TextRoleError(f"{item_id}: visual role/wording/position reason is required.")
        representation = item.get("native_representation")
        if role == "main_copy" and representation not in {"editable", "title_raster"}:
            raise TextRoleError(f"{item_id}: main copy must declare editable or title_raster handling.")
        if role == "illustration_internal" and representation not in {None, "background_internal"}:
            raise TextRoleError(f"{item_id}: illustration-internal text stays in the source background.")
    if accounted_by_items != raw_ids:
        missing = sorted(raw_ids - accounted_by_items)
        raise TextRoleError(f"Inventory items fail to account for raw OCR IDs: {missing}.")
    return sorted(items, key=lambda row: (row["source_bbox_px"][1], row["source_bbox_px"][0], row["id"]))


def compile_review(run_dir_arg: str | Path, raw_ocr_arg: str | Path, review_arg: str | Path,
                   candidates_out_arg: str | Path, decisions_out_arg: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    run_dir = Path(run_dir_arg).expanduser().resolve()
    raw_path = Path(raw_ocr_arg).expanduser().resolve()
    review_path = Path(review_arg).expanduser().resolve()
    manifest, raw_ocr, _source_path, source_hash, raw_hash = _source_context(run_dir, raw_path)
    source = manifest["source"]
    review = read_json(review_path)
    ordered = validate_review(review, source_hash=source_hash, raw_ocr_hash=raw_hash,
                              raw_ocr=raw_ocr, width=source["width_px"], height=source["height_px"])
    review_hash = sha256_file(review_path)
    from selection_gate import _write as write_gate, gate_path, load_gate
    gate = load_gate(run_dir, source_hash)
    gate.update({"text_role_review_path": str(review_path),
                 "text_role_review_sha256": review_hash,
                 "raw_ocr_path": str(raw_path), "raw_ocr_sha256": raw_hash})
    write_gate(gate_path(run_dir), gate)
    compiled_candidates = []
    assignments = []
    for item in ordered:
        role = item["text_role"]
        classification = (item["native_representation"] if role == "main_copy" else "background_internal")
        compiled_candidates.append({
            "id": item["id"], "text_candidate": item["text"], "box_px": item["source_bbox_px"],
            "block_label": "visual_role_reviewed", "text_role": role,
            "source_location": item["source_location"],
            "source_ocr_ids": item["source_ocr_ids"], "source_kind": item["source_kind"],
            "role_review_reason": item["review_reason"], "role_review_reviewer": review["reviewer"],
            "native_representation": item["native_representation"],
        })
        assignments.append({"ids": [item["id"]], "classification": classification,
                            "reason": item["review_reason"], "reviewer": review["reviewer"],
                            "decision_source": "source_bound_contextual_text_role_review"})
    compiled = {
        "schema_version": 1,
        "task": "source_text_role_compiled_ocr",
        "workflow_version": "2.1.1",
        "backend": raw_ocr.get("backend", "unknown"),
        "advisory_only": bool(raw_ocr.get("advisory_only", True)),
        "source": {"sha256": source_hash, "width_px": source["width_px"],
                   "height_px": source["height_px"], "raw_ocr_sha256": raw_hash},
        "text_role_review": {"path": str(review_path), "sha256": review_hash,
                             "source_sha256": source_hash,
                             "raw_ocr_path": str(raw_path), "raw_ocr_sha256": raw_hash,
                             "reviewer": review["reviewer"], "review_status": "reviewed",
                             "whole_source_review": review["whole_source_review"],
                             "item_ids": [item["id"] for item in ordered]},
        "candidates": compiled_candidates,
    }
    candidates_path = Path(candidates_out_arg).expanduser().resolve()
    write_json(candidates_path, compiled)
    compiled_hash = sha256_file(candidates_path)
    decisions = {
        "schema_version": 1, "source_sha256": source_hash,
        "ocr_sha256": compiled_hash, "reviewer": review["reviewer"],
        "role_review_sha256": review_hash, "assignments": assignments,
        "paragraph_groups": [], "supplements": [], "ocr_text_overrides": [],
        "ocr_coverage_review": {"status": "confirmed", "reason": review["whole_source_review"]["notes"]},
        "whole_image_visual_review": {
            "status": "confirmed", "reviewer": review["reviewer"],
            "reason": review["whole_source_review"]["notes"],
            "coverage_artifact_sha256": None,
            "reviewed_region_ids": ["V001"],
            "whole_source_visual_review": True,
            "region_review_batches": [{"region_ids": ["V001"], "outcome": "checked_against_source",
                                       "reason": review["whole_source_review"]["notes"]}],
        },
    }
    decisions_path = Path(decisions_out_arg).expanduser().resolve()
    write_json(decisions_path, decisions)
    return compiled, decisions


def bind_coverage(decisions_arg: str | Path, coverage_arg: str | Path,
                  source_hash: str, ocr_hash: str) -> dict[str, Any]:
    decisions_path = Path(decisions_arg).expanduser().resolve()
    coverage_path = Path(coverage_arg).expanduser().resolve()
    decisions = read_json(decisions_path)
    coverage = read_json(coverage_path)
    if decisions.get("source_sha256") != source_hash or decisions.get("ocr_sha256") != ocr_hash:
        raise TextRoleError("Decisions file does not match the compiled role inventory.")
    if (coverage.get("method") != "whole_image_visual_review"
            or coverage.get("source_sha256") != source_hash
            or coverage.get("ocr_sha256") != ocr_hash):
        raise TextRoleError("Coverage artifact does not match the compiled source and role inventory.")
    decisions["whole_image_visual_review"]["coverage_artifact_sha256"] = sha256_file(coverage_path)
    write_json(decisions_path, decisions)
    return decisions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    draft = sub.add_parser("draft", help="create an editable review ledger from existing OCR; no OCR request")
    draft.add_argument("--run-dir", required=True)
    draft.add_argument("--raw-ocr", required=True)
    draft.add_argument("--output", required=True)
    compile_cmd = sub.add_parser("compile", help="validate and compile the completed visual role ledger")
    compile_cmd.add_argument("--run-dir", required=True)
    compile_cmd.add_argument("--raw-ocr", required=True)
    compile_cmd.add_argument("--review", required=True)
    compile_cmd.add_argument("--candidates-out", required=True)
    compile_cmd.add_argument("--decisions-out", required=True)
    bind = sub.add_parser("bind-coverage", help="bind the generated whole-image overlay into the compiled decisions")
    bind.add_argument("--decisions", required=True)
    bind.add_argument("--coverage", required=True)
    bind.add_argument("--source-sha256", required=True)
    bind.add_argument("--ocr-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "draft":
            result = create_review_template(args.run_dir, args.raw_ocr, args.output)
            print(json.dumps({"status": result["review_status"], "review_file": str(Path(args.output).expanduser().resolve()),
                              "item_count": len(result["items"]), "source_sha256": result["source_sha256"],
                              "raw_ocr_sha256": result["raw_ocr_sha256"]}, ensure_ascii=False))
        elif args.command == "compile":
            compiled, _decisions = compile_review(args.run_dir, args.raw_ocr, args.review,
                                                   args.candidates_out, args.decisions_out)
            print(json.dumps({"status": "reviewed", "item_count": len(compiled["candidates"]),
                              "numberable_count": sum(row["text_role"] == "main_copy" for row in compiled["candidates"]),
                              "review_sha256": compiled["text_role_review"]["sha256"],
                              "candidates": str(Path(args.candidates_out).expanduser().resolve()),
                              "decisions": str(Path(args.decisions_out).expanduser().resolve())}, ensure_ascii=False))
        else:
            decisions = bind_coverage(args.decisions, args.coverage, args.source_sha256, args.ocr_sha256)
            print(json.dumps({"status": decisions["whole_image_visual_review"]["status"],
                              "coverage_sha256": decisions["whole_image_visual_review"]["coverage_artifact_sha256"]}, ensure_ascii=False))
        return 0
    except (TextRoleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
