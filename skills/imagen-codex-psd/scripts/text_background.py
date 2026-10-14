#!/usr/bin/env python3
"""Import an accepted clean background and keep reviewed foreground exceptions separate."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

CLASSES = {"editable", "background_internal", "title_raster"}
ACCEPTED_ASSET_REVIEWS = {"approved", "accepted", "DIRECTOR_SAMPLE_ACCEPTED_WITH_WARNING"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class BackgroundError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackgroundError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BackgroundError(f"JSON root must be an object: {path}")
    return value


def _box(item: dict[str, Any], width: int, height: int) -> tuple[int, int, int, int]:
    box = item.get("source_bbox_px")
    if not isinstance(box, list) or len(box) != 4 or any(type(n) is not int for n in box):
        raise BackgroundError(f"{item.get('id', '?')}: invalid source_bbox_px")
    x, y, w, h = box
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
        raise BackgroundError(f"{item.get('id', '?')}: source bbox outside image")
    return x, y, w, h


def _load_plan(source_path: Path, plan_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], np.ndarray]:
    plan = read_json(plan_path)
    items = plan.get("items")
    source_info = plan.get("source")
    if not isinstance(items, list) or not isinstance(source_info, dict):
        raise BackgroundError("Text plan must contain source metadata and an items array.")
    try:
        image = Image.open(source_path)
        image.load()
    except Exception as exc:
        raise BackgroundError(f"Cannot decode source image: {exc}") from exc
    if image.mode not in {"RGB", "RGBA"}:
        raise BackgroundError(f"Unsupported coordinate image mode {image.mode}; use RGB or RGBA.")
    pixels = np.asarray(image).copy()
    height, width = pixels.shape[:2]
    if source_info.get("coordinate_sha256") != sha256_file(source_path):
        raise BackgroundError("Text plan source hash does not match the coordinate image.")
    if (source_info.get("width_px"), source_info.get("height_px")) != (width, height):
        raise BackgroundError("Text plan dimensions do not match the coordinate image.")
    plan_coordinate = Path(source_info.get("coordinate_path", "")).expanduser()
    if not plan_coordinate.is_absolute():
        plan_coordinate = plan_path.parent / plan_coordinate
    manifest_path = plan_coordinate.resolve().parent / "manifest.json"
    if manifest_path.is_file():
        manifest = read_json(manifest_path)
        workflow_manifest = manifest.get("workflow", {})
        if (workflow_manifest.get("selection_required") is True
                or workflow_manifest.get("version") in {"2.1.0", "2.1.1"}):
            from selection_gate import SelectionError, gate_path, require_ready
            try:
                gate = require_ready(plan_coordinate.resolve().parent, sha256_file(source_path))
            except SelectionError as exc:
                raise BackgroundError(str(exc)) from exc
            workflow = plan.get("workflow")
            if (not isinstance(workflow, dict) or workflow.get("version") != gate.get("workflow_version")
                    or workflow.get("mode") != gate.get("mode")
                    or workflow.get("selection_gate_sha256") != sha256_file(gate_path(plan_coordinate.resolve().parent))):
                raise BackgroundError("Text plan does not match the current explicit selection gate.")
            if gate.get("workflow_version") == "2.1.1":
                role_info = plan.get("text_role_inventory")
                if (not isinstance(role_info, dict)
                        or role_info.get("sha256") != gate.get("text_role_review_sha256")
                        or role_info.get("review_status") != "reviewed"
                        or role_info.get("path") != gate.get("text_role_review_path")):
                    raise BackgroundError("2.1.1 text plan is missing its completed source text-role inventory binding.")
                inventory_ids = role_info.get("item_ids")
                if (not isinstance(inventory_ids, list) or len(inventory_ids) != len(set(inventory_ids))
                        or not set(inventory_ids).issubset({item.get("id") for item in items if isinstance(item, dict)})):
                    raise BackgroundError("Text plan omits one or more reviewed source text inventory items.")
                role_path = Path(role_info["path"])
                if not role_path.is_file() or sha256_file(role_path) != role_info["sha256"]:
                    raise BackgroundError("Source text-role inventory is missing or changed.")
                role_doc = read_json(role_path)
                source_items = {row.get("id"): row for row in role_doc.get("items", []) if isinstance(row, dict)}
                plan_items = {row.get("id"): row for row in items if isinstance(row, dict)}
                for item_id in inventory_ids:
                    source_item, plan_item = source_items.get(item_id), plan_items.get(item_id)
                    if (not isinstance(source_item, dict) or not isinstance(plan_item, dict)
                            or plan_item.get("text") != source_item.get("text")
                            or plan_item.get("source_bbox_px") != source_item.get("source_bbox_px")
                            or plan_item.get("text_role") != source_item.get("text_role")
                            or plan_item.get("source_location") != source_item.get("source_location")):
                        raise BackgroundError(f"Text plan item {item_id} differs from the reviewed source ledger.")
            if gate.get("mode") == "partial":
                selection = plan.get("partial_selection")
                if (not isinstance(selection, dict) or selection.get("status") != "ready"
                        or selection.get("source_sha256") != sha256_file(source_path)
                        or selection.get("raw_ocr_sha256", selection.get("ocr_sha256")) != gate.get("ocr_sha256")
                        or selection.get("selected_ids") != sorted(gate.get("selected_ids", []))):
                    raise BackgroundError("Partial text plan has an invalid or stale selected/preserved partition.")
                by_id = {item["id"]: item for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)}
                selected_ids = set(selection.get("selected_ids", []))
                preserved_ids = set(selection.get("preserved_inventory_ids", selection.get("preserved_ocr_ids", [])))
                editable_ids = {item_id for item_id, item in by_id.items() if item.get("classification") == "editable"}
                role_preserved_ids = {item_id for item_id, item in by_id.items() if item.get("selection_role") == "preserve_in_background"}
                if (not editable_ids.issubset(selected_ids) or role_preserved_ids != preserved_ids
                        or (gate.get("workflow_version") == "2.1.0" and editable_ids != selected_ids)):
                    raise BackgroundError("Partial plan Type layers do not match its explicit selected/preserved ID sets.")
                if selected_ids.intersection(preserved_ids) or not selected_ids.union(preserved_ids).issubset(set(by_id)):
                    raise BackgroundError("Partial plan selected/preserved IDs overlap or are missing from the plan.")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise BackgroundError("Every text-plan item needs an id.")
        if item["id"] in seen:
            raise BackgroundError(f"Duplicate text-plan id: {item['id']}")
        seen.add(item["id"])
        if item.get("classification") not in CLASSES:
            raise BackgroundError(f"{item['id']}: invalid text classification")
        if item.get("classification_status") != "visually_reviewed":
            raise BackgroundError(f"{item['id']}: classification is still pending visual review")
        _box(item, width, height)
    return plan, items, pixels


def _safe_output_dir(out_arg: str | Path) -> Path:
    out = Path(out_arg).expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        raise BackgroundError(f"Output directory is not empty; refusing to overwrite: {out}")
    out.mkdir(parents=True, exist_ok=True)
    return out


def extract_title_source_rgba(source_pixels: np.ndarray, alpha_mask: np.ndarray) -> np.ndarray:
    """Return original RGB pixels under an explicit title alpha; this does not approve a title."""
    if source_pixels.ndim != 3 or source_pixels.shape[2] not in {3, 4}:
        raise BackgroundError("Title source pixels must be RGB or RGBA.")
    if alpha_mask.ndim != 2 or alpha_mask.shape != source_pixels.shape[:2]:
        raise BackgroundError("Title alpha mask must match the full source canvas.")
    alpha = np.clip(alpha_mask, 0, 255).astype(np.uint8)
    rgb = source_pixels[:, :, :3].astype(np.uint8)
    return np.dstack((rgb, alpha))


def _title_pending(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"text_item_id": item["id"], "status": "PENDING_MANUAL_EXTRACTION", "path": None,
             "source_bbox_px": item["source_bbox_px"],
             "note": "Clean-base import does not extract or approve title pixels."}
            for item in items if item["classification"] == "title_raster"]


def _review_binding(review: dict[str, Any], source_hash: str, plan_hash: str, base_hash: str) -> None:
    expected = {"source_sha256": source_hash, "text_plan_sha256": plan_hash, "clean_base_sha256": base_hash}
    for key, value in expected.items():
        if review.get(key) != value:
            raise BackgroundError(f"Background review {key} does not match the supplied input.")


def _unique_ids(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise BackgroundError(f"Background review {field} must be an array of IDs.")
    if len(value) != len(set(value)):
        raise BackgroundError(f"Background review {field} contains duplicate IDs.")
    return value


def _validate_accepted_review(review: dict[str, Any], items: list[dict[str, Any]],
                              plan: dict[str, Any]) -> list[str]:
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise BackgroundError("Accepted background review needs a reviewer.")
    text_review = review.get("text_review")
    structure = review.get("nontext_structure_review")
    if not isinstance(text_review, dict) or text_review.get("status") != "accepted":
        raise BackgroundError("Accepted background review needs accepted text_review.")
    if not isinstance(structure, dict) or structure.get("status") != "accepted":
        raise BackgroundError("Accepted background review needs accepted nontext_structure_review.")
    expected_main = {i["id"] for i in items if i["classification"] in {"editable", "title_raster"}}
    expected_internal = {i["id"] for i in items if i["classification"] == "background_internal"}
    cleared = set(_unique_ids(text_review.get("cleared_main_text_ids"), "cleared_main_text_ids"))
    preserved = set(_unique_ids(text_review.get("preserved_internal_text_ids"), "preserved_internal_text_ids"))
    if cleared != expected_main:
        raise BackgroundError("Background review must account for every editable and title_raster text ID.")
    if preserved != expected_internal:
        raise BackgroundError("Background review must account for every background_internal text ID.")
    partial = plan.get("partial_selection")
    if isinstance(partial, dict) and partial.get("status") == "ready":
        expected_selected = set(partial.get("selected_ids", []))
        expected_preserved = set(partial.get("preserved_inventory_ids", partial.get("preserved_ocr_ids", [])))
        if expected_main != expected_selected or expected_internal != expected_preserved:
            raise BackgroundError("Partial background review must clear only selected IDs and preserve every unselected OCR ID.")
        by_id = {item["id"]: item for item in items}
        for field, ids, detail_fields, booleans in (
                ("cleared_selection_items", expected_selected, ("text", "source_bbox_px"),
                 ("visible_in_source", "removed_from_clean_base")),
                ("preserved_selection_items", expected_preserved, ("text", "source_bbox_px"),
                 ("visible_in_source", "retained_in_clean_base", "position_matches_source"))):
            records = text_review.get(field)
            if not isinstance(records, list):
                raise BackgroundError(f"Partial background review needs {field} for every selected or preserved OCR block.")
            by_review_id = {row.get("id"): row for row in records if isinstance(row, dict)}
            if len(by_review_id) != len(records) or set(by_review_id) != ids:
                raise BackgroundError(f"Partial background review {field} must exactly cover its expected IDs.")
            for item_id in ids:
                row, source_item = by_review_id[item_id], by_id[item_id]
                for key in detail_fields:
                    if row.get(key) != source_item.get(key):
                        raise BackgroundError(f"Partial background review {field} has stale {key} for {item_id}.")
                if any(row.get(key) is not True for key in booleans):
                    raise BackgroundError(f"Partial background review {field} lacks visual confirmation for {item_id}.")
        identity = text_review.get("illustration_text_identity")
        if (not isinstance(identity, dict) or identity.get("checked") is not True
                or identity.get("identities_preserved") is not True
                or not isinstance(identity.get("summary"), str) or not identity["summary"].strip()):
            raise BackgroundError("Partial background review must confirm illustration-internal text identity and position.")
    missing = _unique_ids(structure.get("missing_from_clean_base"), "missing_from_clean_base")
    summary = structure.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise BackgroundError("Accepted structure review needs a summary.")
    if not isinstance(text_review.get("summary"), str) or not text_review["summary"].strip():
        raise BackgroundError("Accepted text review needs a summary.")
    for visual_id in missing:
        if not SAFE_ID.fullmatch(visual_id):
            raise BackgroundError(f"Unsafe missing foreground ID: {visual_id}")
    return missing


def _manifest_path(manifest_path: Path, raw_path: Any, field: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise BackgroundError(f"Foreground exception needs {field}.")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = manifest_path.parent / path
    path = path.resolve()
    if not path.is_file():
        raise BackgroundError(f"Foreground exception file does not exist: {path}")
    return path


def _sheet_box(value: Any, size: tuple[int, int], label: str) -> tuple[int, int, int, int]:
    width, height = size
    if not isinstance(value, list) or len(value) != 4 or any(type(v) is not int for v in value):
        raise BackgroundError(f"{label} must be integer [x, y, width, height].")
    x, y, w, h = value
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
        raise BackgroundError(f"{label} is outside its asset sheet.")
    return x, y, w, h


def _fit_alpha_to_box(asset: Image.Image, box: tuple[int, int, int, int]) -> tuple[Image.Image, dict[str, Any]]:
    x, y, width, height = box
    rgba = asset.convert("RGBA")
    alpha = rgba.getchannel("A")
    visible = alpha.getbbox()
    if visible is None:
        raise BackgroundError("Foreground alpha asset is empty.")
    cropped = rgba.crop(visible)
    scale = min(width / cropped.width, height / cropped.height)
    fitted_size = (max(1, min(width, round(cropped.width * scale))),
                   max(1, min(height, round(cropped.height * scale))))
    fitted = cropped.resize(fitted_size, Image.Resampling.LANCZOS)
    offset = ((width - fitted.width) // 2, (height - fitted.height) // 2)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(fitted, offset)
    return canvas, {
        "mode": "contain",
        "source_box_px": [x, y, width, height],
        "visible_box_px": [x + offset[0], y + offset[1], fitted.width, fitted.height],
        "display_pixels_px": list(fitted.size),
        "uniform_scale_from_visible_alpha": round(scale, 8),
        "z_order": "above clean background, below native text",
    }


def _load_exceptions(manifest_path: Path, source_hash: str, base_hash: str,
                     expected_missing: list[str], canvas_size: tuple[int, int]) -> list[dict[str, Any]]:
    manifest = read_json(manifest_path)
    inputs = manifest.get("inputs", {})
    identity = inputs.get("identity_source", {}) if isinstance(inputs, dict) else {}
    clean_base = manifest.get("clean_base", {})
    if not isinstance(identity, dict) or identity.get("sha256") != source_hash:
        raise BackgroundError("Foreground exception source hash does not match the coordinate source.")
    if not isinstance(clean_base, dict) or clean_base.get("sha256") != base_hash:
        raise BackgroundError("Foreground exception clean-base hash does not match the supplied base.")
    records = manifest.get("foreground_exceptions")
    if not isinstance(records, list):
        raise BackgroundError("Foreground exception manifest needs a foreground_exceptions array.")
    inputs = inputs if isinstance(inputs, dict) else {}
    imagegen = manifest.get("imagegen_output", {})
    processing = manifest.get("processing", {})
    provenance = {"exception_manifest_path": str(manifest_path),
                  "exception_manifest_sha256": sha256_file(manifest_path),
                  "backend": manifest.get("backend"), "identity_source": identity,
                  "location_context": inputs.get("location_context"),
                  "imagegen_output": imagegen, "processing": processing}
    if isinstance(imagegen, dict) and imagegen.get("workspace_asset_sheet"):
        sheet_path = _manifest_path(manifest_path, imagegen["workspace_asset_sheet"], "workspace_asset_sheet")
        sheet_hash = sha256_file(sheet_path)
        if imagegen.get("workspace_sha256") != sheet_hash or imagegen.get("generated_sha256") != sheet_hash:
            raise BackgroundError("Image-generation asset-sheet hash does not match its recorded source.")
        provenance["workspace_asset_sheet"] = {"path": str(sheet_path), "sha256": sheet_hash}
    if isinstance(processing, dict) and processing.get("alpha_asset_path"):
        processed_asset = _manifest_path(manifest_path, processing["alpha_asset_path"], "processing.alpha_asset_path")
        if sha256_file(processed_asset) != processing.get("alpha_asset_sha256"):
            raise BackgroundError("Processed alpha asset hash does not match its recorded source.")
    ids: list[str] = []
    validated: list[dict[str, Any]] = []
    sheet_batches: dict[str, dict[str, Any]] = {}
    processed_sheets: dict[str, Image.Image] = {}
    width, height = canvas_size
    for record in records:
        if not isinstance(record, dict):
            raise BackgroundError("Each foreground exception must be an object.")
        visual_id = record.get("id")
        if not isinstance(visual_id, str) or not SAFE_ID.fullmatch(visual_id):
            raise BackgroundError("Foreground exception ID is invalid.")
        if visual_id in ids:
            raise BackgroundError(f"Duplicate foreground exception ID: {visual_id}")
        ids.append(visual_id)
        if record.get("type") != "missing_from_clean_base":
            raise BackgroundError(f"{visual_id}: only missing_from_clean_base exceptions are supported.")
        if not isinstance(record.get("reason"), str) or not record["reason"].strip():
            raise BackgroundError(f"{visual_id}: a missing-object reason is required.")
        box = record.get("source_bbox_px")
        item = {"id": visual_id, "source_bbox_px": box}
        target_box = _box(item, width, height)
        sheet_provenance = None
        split_entry = None
        sheet_ref = record.get("asset_sheet_ref")
        if sheet_ref is not None:
            if not isinstance(sheet_ref, dict):
                raise BackgroundError(f"{visual_id}: asset_sheet_ref must be an object.")
            sheet_id, cell_id = sheet_ref.get("sheet_id"), sheet_ref.get("cell_id")
            if not isinstance(sheet_id, str) or not SAFE_ID.fullmatch(sheet_id) or cell_id != visual_id:
                raise BackgroundError(f"{visual_id}: sheet/cell IDs must bind this object inventory ID.")
            generated_path = _manifest_path(manifest_path, sheet_ref.get("generated_sheet_path"),
                                            "asset_sheet_ref.generated_sheet_path")
            processed_path = _manifest_path(manifest_path, sheet_ref.get("processed_sheet_path"),
                                             "asset_sheet_ref.processed_sheet_path")
            cell_map_path = _manifest_path(manifest_path, sheet_ref.get("cell_map_path"),
                                           "asset_sheet_ref.cell_map_path")
            split_manifest_path = _manifest_path(manifest_path, sheet_ref.get("split_manifest_path"),
                                                 "asset_sheet_ref.split_manifest_path")
            generated_hash, processed_hash = sha256_file(generated_path), sha256_file(processed_path)
            cell_map_hash, split_manifest_hash = sha256_file(cell_map_path), sha256_file(split_manifest_path)
            if sheet_ref.get("generated_sheet_sha256") != generated_hash:
                raise BackgroundError(f"{visual_id}: generated asset sheet hash mismatch.")
            if sheet_ref.get("processed_sheet_sha256") != processed_hash:
                raise BackgroundError(f"{visual_id}: processed alpha sheet hash mismatch.")
            if sheet_ref.get("cell_map_sha256") != cell_map_hash:
                raise BackgroundError(f"{visual_id}: cell-map hash mismatch.")
            if sheet_ref.get("split_manifest_sha256") != split_manifest_hash:
                raise BackgroundError(f"{visual_id}: split-manifest hash mismatch.")
            cell_map, split_manifest = read_json(cell_map_path), read_json(split_manifest_path)
            cell_records = cell_map.get("cells", [])
            mapped_cell = next((entry for entry in cell_records if isinstance(entry, dict) and entry.get("id") == cell_id), None)
            if (cell_map.get("schema_version") != 1 or cell_map.get("processed_sheet_sha256") != processed_hash
                    or cell_map.get("generated_sheet_sha256") != generated_hash or mapped_cell is None):
                raise BackgroundError(f"{visual_id}: cell map does not bind the generated and processed sheet/object ID.")
            if mapped_cell.get("bbox_px") != sheet_ref.get("cell_bbox_px"):
                raise BackgroundError(f"{visual_id}: cell-map bbox differs from exception manifest.")
            if (split_manifest.get("schema_version") != 1 or split_manifest.get("source_sha256") != processed_hash
                    or split_manifest.get("generated_sheet_sha256") != generated_hash
                    or split_manifest.get("cell_map_sha256") != cell_map_hash):
                raise BackgroundError(f"{visual_id}: split manifest does not bind the same sheet/cell map.")
            split_assets = split_manifest.get("assets", [])
            split_entry = next((entry for entry in split_assets if isinstance(entry, dict) and entry.get("id") == visual_id), None)
            if (split_entry is None or split_entry.get("cell_bbox_px") != sheet_ref.get("cell_bbox_px")
                    or split_entry.get("component_bbox_px") != sheet_ref.get("component_bbox_px")):
                raise BackgroundError(f"{visual_id}: split report has no matching ID-to-cell/component entry.")
            if processed_hash not in processed_sheets:
                try:
                    with Image.open(processed_path) as sheet:
                        sheet.load()
                        if "A" not in sheet.getbands():
                            raise BackgroundError(f"{visual_id}: processed sheet has no alpha channel.")
                        processed_sheets[processed_hash] = sheet.convert("RGBA")
                except BackgroundError:
                    raise
                except Exception as exc:
                    raise BackgroundError(f"{visual_id}: cannot decode processed sheet: {exc}") from exc
            processed_sheet = processed_sheets[processed_hash]
            cell_box = _sheet_box(sheet_ref.get("cell_bbox_px"), processed_sheet.size,
                                  f"{visual_id} cell_bbox_px")
            component_box = _sheet_box(sheet_ref.get("component_bbox_px"), processed_sheet.size,
                                       f"{visual_id} component_bbox_px")
            cx, cy, cw, ch = cell_box
            px, py, pw, ph = component_box
            if px < cx or py < cy or px + pw > cx + cw or py + ph > cy + ch:
                raise BackgroundError(f"{visual_id}: alpha component is outside its mapped cell.")
            batch = sheet_batches.setdefault(sheet_id, {
                "generated_path": str(generated_path), "generated_hash": generated_hash,
                "processed_path": str(processed_path), "processed_hash": processed_hash,
                "cell_map_path": str(cell_map_path), "cell_map_hash": cell_map_hash,
                "split_manifest_path": str(split_manifest_path), "split_manifest_hash": split_manifest_hash,
                "cells": []})
            if (batch["generated_path"], batch["generated_hash"], batch["processed_path"], batch["processed_hash"],
                    batch["cell_map_path"], batch["cell_map_hash"], batch["split_manifest_path"], batch["split_manifest_hash"]) != (
                    str(generated_path), generated_hash, str(processed_path), processed_hash,
                    str(cell_map_path), cell_map_hash, str(split_manifest_path), split_manifest_hash):
                raise BackgroundError(f"{visual_id}: one sheet_id refers to inconsistent sheet files.")
            if len(batch["cells"]) >= 9:
                raise BackgroundError(f"{visual_id}: one sparse sheet may map at most nine objects.")
            for other_id, other_box in batch["cells"]:
                ox, oy, ow, oh = other_box
                if cx < ox + ow and ox < cx + cw and cy < oy + oh and oy < cy + ch:
                    raise BackgroundError(f"{visual_id}: cell overlaps mapped object {other_id}.")
            batch["cells"].append((visual_id, cell_box))
            sheet_provenance = {"sheet_id": sheet_id, "cell_id": cell_id,
                                "generated_sheet_path": str(generated_path),
                                "generated_sheet_sha256": generated_hash,
                                "processed_sheet_path": str(processed_path),
                                "processed_sheet_sha256": processed_hash,
                                "cell_map_path": str(cell_map_path), "cell_map_sha256": cell_map_hash,
                                "split_manifest_path": str(split_manifest_path),
                                "split_manifest_sha256": split_manifest_hash,
                                "cell_bbox_px": list(cell_box), "component_bbox_px": list(component_box)}
        review = record.get("visual_review", {})
        if not isinstance(review, dict) or review.get("status") not in ACCEPTED_ASSET_REVIEWS:
            raise BackgroundError(f"{visual_id}: foreground visual review is not accepted.")
        if not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
            raise BackgroundError(f"{visual_id}: accepted visual review needs a reviewer.")
        asset_path = _manifest_path(manifest_path, record.get("alpha_asset_path"), "alpha_asset_path")
        actual_hash = sha256_file(asset_path)
        if record.get("alpha_asset_sha256") != actual_hash:
            raise BackgroundError(f"{visual_id}: alpha asset hash mismatch.")
        if sheet_provenance is not None:
            split_asset_path = _manifest_path(Path(sheet_provenance["split_manifest_path"]),
                                              split_entry.get("path"), "split_manifest.assets.path")
            if split_asset_path != asset_path or split_entry.get("sha256") != actual_hash:
                raise BackgroundError(f"{visual_id}: split report does not bind this alpha asset path/hash.")
        try:
            asset = Image.open(asset_path)
            asset.load()
        except Exception as exc:
            raise BackgroundError(f"{visual_id}: cannot decode alpha asset: {exc}") from exc
        if "A" not in asset.getbands():
            raise BackgroundError(f"{visual_id}: alpha asset has no transparency channel.")
        alpha = asset.convert("RGBA").getchannel("A")
        alpha_min, alpha_max = alpha.getextrema()
        if alpha_max == 0:
            raise BackgroundError(f"{visual_id}: alpha asset is empty.")
        if alpha_min != 0:
            raise BackgroundError(f"{visual_id}: alpha asset must contain transparent and visible pixels.")
        if sheet_provenance is not None:
            cx, cy, cw, ch = sheet_provenance["cell_bbox_px"]
            processed = processed_sheets[sheet_provenance["processed_sheet_sha256"]]
            extracted = processed.crop((cx, cy, cx + cw, cy + ch))
            if extracted.size != asset.size or extracted.tobytes() != asset.convert("RGBA").tobytes():
                raise BackgroundError(f"{visual_id}: alpha asset does not match its recorded sheet cell.")
            visible = extracted.getchannel("A").getbbox()
            if visible is None or [cx + visible[0], cy + visible[1], visible[2] - visible[0], visible[3] - visible[1]] != sheet_provenance["component_bbox_px"]:
                raise BackgroundError(f"{visual_id}: recorded visible-component bbox does not match its sheet cell.")
        fit_layer, placement = _fit_alpha_to_box(asset, target_box)
        declared_placement = record.get("placement", {})
        if not isinstance(declared_placement, dict) or declared_placement.get("mode") != "contain":
            raise BackgroundError(f"{visual_id}: reviewed placement must use contain mode.")
        validated.append({"id": visual_id, "type": record["type"], "reason": record["reason"],
                          "source_bbox_px": list(target_box), "source_box_px": list(target_box),
                          "source_bbox_is_initial_placement": bool(record.get("source_bbox_is_initial_placement")),
                          "input_asset_path": asset_path, "asset_sha256": actual_hash,
                          "asset_size_px": list(asset.size), "visual_review": review,
                          **({"asset_sheet_ref": sheet_provenance} if sheet_provenance is not None else {}),
                          "fit_layer": fit_layer, "placement": placement, "provenance": provenance})
    if set(ids) != set(expected_missing) or len(ids) != len(expected_missing):
        raise BackgroundError("Foreground exception IDs must exactly match missing_from_clean_base in the accepted review.")
    return validated


def _write_pending_result(out_dir: Path, source_path: Path, plan_path: Path, base_path: Path,
                          source_pixels: np.ndarray, items: list[dict[str, Any]],
                          review_path: Path | None, exceptions_path: Path | None,
                          pending_reason: str) -> dict[str, Any]:
    width, height = source_pixels.shape[1], source_pixels.shape[0]
    titles = _title_pending(items)
    title_path = out_dir / "title-layers.json"
    write_json(title_path, {"schema_version": 1, "source_sha256": sha256_file(source_path), "title_layers": titles})
    result = {
        "schema_version": 2,
        "status": "PENDING_BACKGROUND_REVIEW",
        "source_sha256": sha256_file(source_path),
        "text_plan_sha256": sha256_file(plan_path),
        "clean_base_sha256": sha256_file(base_path),
        "source_size_px": {"width": width, "height": height},
        "background": None,
        "background_ready": False,
        "foreground_asset_manifest": None,
        "title_layers_path": str(title_path),
        "pending_titles": [x["text_item_id"] for x in titles],
        "ready_for_next_stage": False,
        "ready_for_psd": False,
        "pending_reason": pending_reason,
        "review_path": str(review_path) if review_path else None,
        "foreground_exceptions_path": str(exceptions_path) if exceptions_path else None,
        "notes": ["Unreviewed clean-base pixels were not imported or replaced with the source image."],
        "next_stage": {"status": "blocked", "pending": ["accepted-background-review"] +
                       [x["text_item_id"] for x in titles]},
    }
    write_json(out_dir / "background-build.json", result)
    return result


def build_background(source_arg: str | Path, plan_arg: str | Path, clean_base_arg: str | Path,
                     out_arg: str | Path, review_arg: str | Path | None = None,
                     exceptions_arg: str | Path | None = None) -> dict[str, Any]:
    source_path = Path(source_arg).expanduser().resolve()
    plan_path = Path(plan_arg).expanduser().resolve()
    base_path = Path(clean_base_arg).expanduser().resolve()
    if not source_path.is_file() or not plan_path.is_file() or not base_path.is_file():
        raise BackgroundError("Coordinate source, text plan, and clean-base image must exist.")
    plan, items, source_pixels = _load_plan(source_path, plan_path)
    try:
        base_image = Image.open(base_path)
        base_image.load()
    except Exception as exc:
        raise BackgroundError(f"Cannot decode clean-base image: {exc}") from exc
    if base_image.format != "PNG" or base_image.mode not in {"RGB", "RGBA"}:
        raise BackgroundError("Clean base must be an RGB or RGBA PNG.")
    width, height = source_pixels.shape[1], source_pixels.shape[0]
    if base_image.size != (width, height):
        raise BackgroundError("Clean-base dimensions do not match the coordinate source.")
    if np.array_equal(np.asarray(base_image.convert("RGB")), source_pixels[:, :, :3]):
        raise BackgroundError("Clean base is pixel-identical to the coordinate source; refusing an unclean base.")
    source_hash, plan_hash, base_hash = sha256_file(source_path), sha256_file(plan_path), sha256_file(base_path)
    out_dir = _safe_output_dir(out_arg)
    review_path = Path(review_arg).expanduser().resolve() if review_arg else None
    exceptions_path = Path(exceptions_arg).expanduser().resolve() if exceptions_arg else None
    if review_path is None:
        return _write_pending_result(out_dir, source_path, plan_path, base_path, source_pixels, items,
                                     review_path, exceptions_path, "No accepted background review was supplied.")
    if not review_path.is_file():
        raise BackgroundError(f"Background review not found: {review_path}")
    review = read_json(review_path)
    _review_binding(review, source_hash, plan_hash, base_hash)
    if review.get("status") != "accepted":
        return _write_pending_result(out_dir, source_path, plan_path, base_path, source_pixels, items,
                                     review_path, exceptions_path, "Background review has not been accepted.")
    missing_ids = _validate_accepted_review(review, items, plan)
    if exceptions_path is not None and not exceptions_path.is_file():
        raise BackgroundError(f"Foreground exception manifest not found: {exceptions_path}")
    if exceptions_path:
        exceptions = _load_exceptions(exceptions_path, source_hash, base_hash, missing_ids, (width, height))
    elif missing_ids:
        exceptions = []
    else:
        exceptions = []

    background_path = out_dir / "background.png"
    shutil.copyfile(base_path, background_path)
    imported = Image.open(background_path)
    imported.load()
    if not np.array_equal(np.asarray(imported), np.asarray(base_image)):
        raise BackgroundError("Imported background pixels differ from the accepted clean base.")

    layer_dir = out_dir / "foreground-assets"
    layer_records: list[dict[str, Any]] = []
    preview = base_image.convert("RGBA")
    for exception in exceptions:
        target = layer_dir / f"{exception['id']}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(exception["input_asset_path"], target)
        preview.alpha_composite(exception["fit_layer"], tuple(exception["source_bbox_px"][:2]))
        layer_records.append({
            "visual_unit_id": exception["id"],
            "type": exception["type"],
            "reason": exception["reason"],
            "asset_path": str(target.relative_to(out_dir)),
            "asset_sha256": sha256_file(target),
            "asset_size_px": exception["asset_size_px"],
            "source_bbox_px": exception["source_bbox_px"],
            "source_box_px": exception["source_box_px"],
            "z_order": exception["placement"]["z_order"],
            "placement": exception["placement"],
            "source_sha256": source_hash,
            "clean_base_sha256": base_hash,
            "visual_review": exception["visual_review"],
            "provenance": exception["provenance"],
        })
    layer_manifest_path = out_dir / "foreground-assets.json"
    layer_manifest = {"schema_version": 1, "status": "accepted_with_warning" if any(
        x["visual_review"].get("status") == "DIRECTOR_SAMPLE_ACCEPTED_WITH_WARNING" for x in exceptions
    ) else "accepted", "source_sha256": source_hash, "text_plan_sha256": plan_hash,
        "clean_base_sha256": base_hash, "foreground_assets": layer_records}
    write_json(layer_manifest_path, layer_manifest)
    preview_path = out_dir / "preview.png"
    preview.save(preview_path, format="PNG")
    title_layers = _title_pending(items)
    title_path = out_dir / "title-layers.json"
    write_json(title_path, {"schema_version": 1, "source_sha256": source_hash, "title_layers": title_layers})
    pending_exceptions = sorted(set(missing_ids) - {x["id"] for x in exceptions})
    pending_titles = [x["text_item_id"] for x in title_layers]
    pending_next = pending_titles + pending_exceptions
    if pending_titles and pending_exceptions:
        status = "BACKGROUND_IMPORTED_CONTENT_PENDING"
    elif pending_titles:
        status = "BACKGROUND_IMPORTED_TITLES_PENDING"
    elif pending_exceptions:
        status = "BACKGROUND_IMPORTED_EXCEPTIONS_PENDING"
    else:
        status = "BACKGROUND_READY_FOR_TEXT_ASSEMBLY"
    result = {
        "schema_version": 2,
        "status": status,
        "source_sha256": source_hash,
        "text_plan_sha256": plan_hash,
        "partial_selection": plan.get("partial_selection"),
        "text_role_review_sha256": (plan.get("text_role_inventory") or {}).get("sha256"),
        "clean_base_sha256": base_hash,
        "source_size_px": {"width": width, "height": height},
        "background": {"path": str(background_path), "sha256": sha256_file(background_path),
                       "pixel_identical_to_clean_base": True},
        "background_ready": True,
        "background_review": {"path": str(review_path), "sha256": sha256_file(review_path),
                              "reviewer": review["reviewer"], "status": review["status"],
                              "retained_item_ids": sorted({row.get("id") for row in review.get("text_review", {}).get("preserved_selection_items", []) if isinstance(row, dict)}),
                              "cleared_item_ids": sorted({row.get("id") for row in review.get("text_review", {}).get("cleared_selection_items", []) if isinstance(row, dict)})},
        "foreground_exceptions": {"path": str(exceptions_path) if exceptions_path else None,
                                   "sha256": sha256_file(exceptions_path) if exceptions_path else None,
                                   "asset_manifest_path": str(layer_manifest_path),
                                   "asset_manifest_sha256": sha256_file(layer_manifest_path),
                                   "expected_missing_ids": missing_ids,
                                   "included_ids": [x["visual_unit_id"] for x in layer_records],
                                   "pending_ids": pending_exceptions},
        "preview": {"path": str(preview_path), "sha256": sha256_file(preview_path),
                    "based_on_clean_base_sha256": base_hash, "status": "review_preview_only"},
        "title_layers_path": str(title_path),
        "pending_titles": pending_titles,
        "ready_for_next_stage": not pending_next,
        "ready_for_psd": False,
        "next_stage": {"status": "ready_for_text_assembly" if not pending_next else "blocked",
                       "pending": pending_next},
        "pixel_validation": {"background_matches_clean_base": True,
                             "background_sha256_equals_clean_base_sha256": sha256_file(background_path) == base_hash},
        "notes": ["The clean base is imported as a full-canvas image; no text-bbox source pixels are pasted.",
                  "Foreground exceptions remain separate full-resolution alpha assets; preview scaling does not alter them.",
                  "PSD creation and Photoshop acceptance are not part of this output."]
    }
    write_json(out_dir / "background-build.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="defaults to source.coordinate_path from text plan")
    parser.add_argument("--text-plan", required=True)
    parser.add_argument("--clean-base", required=True, help="accepted full-canvas RGB/RGBA PNG")
    parser.add_argument("--background-review", help="hash-bound visual review JSON; without it result remains pending")
    parser.add_argument("--foreground-exceptions", help="optional reviewed missing-object exception manifest")
    parser.add_argument("--out-dir", required=True, help="must be a new or empty directory")
    args = parser.parse_args(argv)
    try:
        plan_path = Path(args.text_plan).expanduser().resolve()
        plan = read_json(plan_path)
        source = args.source or plan.get("source", {}).get("coordinate_path")
        if not source:
            raise BackgroundError("No coordinate source path in text plan; pass --source.")
        result = build_background(source, plan_path, args.clean_base, args.out_dir,
                                  args.background_review, args.foreground_exceptions)
        print(json.dumps({"status": result["status"], "background_ready": result.get("background_ready", False),
                          "background": result.get("background", {}).get("path") if result.get("background") else None,
                          "ready_for_psd": result.get("ready_for_psd", False)}, ensure_ascii=False))
        return 0
    except (BackgroundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
