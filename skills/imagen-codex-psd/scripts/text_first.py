#!/usr/bin/env python3
"""Minimal text-first poster preparation and OCR text-plan builder."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw

CATEGORIES = {"editable", "background_internal", "title_raster"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
WORKFLOW_VERSION = "2.1.1"


class PlanError(ValueError):
    """Input evidence is invalid or insufficient for the requested plan."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _safe_stem(stem: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
    return (value or "poster")[:48]


def prepare_image(image_arg: str | Path, out_root_arg: str | Path) -> Path:
    """Copy one image, create the coordinate PNG, and bind both with hashes."""
    source = Path(image_arg).expanduser().resolve()
    if not source.is_file():
        raise PlanError(f"Input image not found: {source}")
    if source.suffix.lower() not in IMAGE_EXTENSIONS:
        raise PlanError("Input must be one PNG, JPEG, or WebP image.")

    original_sha = sha256_file(source)
    out_root = Path(out_root_arg).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = out_root / f"{stamp}-{_safe_stem(source.stem)}-{original_sha[:8]}"
    run_dir = base
    suffix = 2
    while run_dir.exists():
        run_dir = Path(f"{base}-{suffix}")
        suffix += 1
    run_dir.mkdir(parents=True)

    input_dir = run_dir / "input"
    input_dir.mkdir()
    preserved = input_dir / source.name
    shutil.copy2(source, preserved)
    if sha256_file(preserved) != original_sha:
        raise PlanError("Copied input hash does not match the selected image.")

    coordinate = run_dir / "source.png"
    with Image.open(preserved) as image:
        image.load()
        width, height = image.size
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        mode = image.mode
        if source.suffix.lower() == ".png":
            # Preserve PNG bytes and their exact OCR lineage when already prepared.
            shutil.copy2(preserved, coordinate)
        else:
            image.convert("RGBA" if has_alpha else "RGB").save(coordinate, format="PNG")
    with Image.open(coordinate) as image:
        if image.size != (width, height):
            raise PlanError("Coordinate PNG changed the source pixel dimensions.")

    manifest = {
        "schema_version": 1,
        "task": "text_first_input",
        "workflow": {"version": WORKFLOW_VERSION, "selection_required": True},
        "source": {
            "original_path": str(source),
            "preserved_input_path": str(preserved),
            "original_sha256": original_sha,
            "coordinate_path": str(coordinate),
            "coordinate_sha256": sha256_file(coordinate),
            "coordinate_format": "png",
            "width_px": width,
            "height_px": height,
            "original_mode": mode,
        },
        "coordinates": {
            "authority": "source.png pixel grid",
            "unit": "px",
            "origin": "top-left",
            "box_format": "[x, y, width, height]",
        },
    }
    write_json(run_dir / "manifest.json", manifest)
    from selection_gate import initialize
    initialize(run_dir, manifest["source"]["coordinate_sha256"])
    return run_dir


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"Cannot read JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanError(f"JSON root must be an object: {path}")
    return value


def build_coverage_candidates(run_dir_arg: str | Path, ocr_arg: str | Path,
                              detector_report_arg: str | Path, output_arg: str | Path,
                              overlay_arg: str | Path | None = None) -> dict[str, Any]:
    """Compare saved DBNet regions with OCR boxes; emit review-only regions and an overlay."""
    run_dir = Path(run_dir_arg).expanduser().resolve()
    manifest = _read_json(run_dir / "manifest.json")
    source = manifest.get("source", {})
    coordinate = Path(source.get("coordinate_path", ""))
    if not coordinate.is_absolute():
        coordinate = run_dir / coordinate
    if not coordinate.is_file() or sha256_file(coordinate) != source.get("coordinate_sha256"):
        raise PlanError("Prepared coordinate image is missing or its hash changed.")
    source_hash = sha256_file(coordinate)
    from selection_gate import SelectionError, require_ready
    try:
        gate = require_ready(run_dir, source_hash)
    except SelectionError as exc:
        raise PlanError(str(exc)) from exc
    width, height = source.get("width_px"), source.get("height_px")

    ocr_path = Path(ocr_arg).expanduser().resolve()
    ocr = _read_json(ocr_path)
    if ocr.get("source", {}).get("sha256") != source_hash:
        raise PlanError("OCR artifact belongs to another coordinate image.")
    if ocr.get("source", {}).get("width_px") != width or ocr.get("source", {}).get("height_px") != height:
        raise PlanError("OCR dimensions do not match the prepared coordinate image.")
    map_ocr_hash = ocr.get("source", {}).get("raw_ocr_sha256") or sha256_file(ocr_path)
    if gate.get("mode") == "partial" and map_ocr_hash != gate.get("ocr_sha256"):
        raise PlanError("OCR artifact changed after the partial-mode numbered selection; show a fresh map before continuing.")

    detector_path = Path(detector_report_arg).expanduser().resolve()
    detector = _read_json(detector_path)
    if detector.get("source_sha256") != source_hash:
        raise PlanError("DBNet report belongs to another coordinate image.")
    detector_size = detector.get("source_size_px", {})
    if detector_size.get("width") != width or detector_size.get("height") != height:
        raise PlanError("DBNet report dimensions do not match the prepared coordinate image.")
    masks = detector.get("masks", {})
    raw_mask_path = Path(masks.get("raw", ""))
    if not raw_mask_path.is_absolute():
        raw_mask_path = detector_path.parent / raw_mask_path
    if not raw_mask_path.is_file():
        raise PlanError("Saved DBNet raw mask is missing.")
    with Image.open(raw_mask_path) as mask_image:
        mask_image.load()
        raw_mask = mask_image.convert("L")
    if raw_mask.size != (width, height):
        raise PlanError("Saved DBNet raw mask dimensions do not match the source.")

    candidates = ocr.get("candidates")
    if not isinstance(candidates, list):
        raise PlanError("OCR artifact must contain a candidates array.")
    ocr_coverage = Image.new("L", (width, height), 0)
    draw_ocr = ImageDraw.Draw(ocr_coverage)
    ocr_ids: list[tuple[str, list[int]]] = []
    for item in candidates:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise PlanError("Every OCR candidate needs an id before coverage comparison.")
        box = _validate_box(item.get("box_px"), width, height, item["id"])
        x, y, w, h = box
        draw_ocr.rectangle((x, y, x + w - 1, y + h - 1), fill=255)
        ocr_ids.append((item["id"], box))
    uncovered_mask = ImageChops.multiply(raw_mask, ImageChops.invert(ocr_coverage))
    report_boxes = detector.get("detection_boxes")
    if not isinstance(report_boxes, list):
        raise PlanError("DBNet report must contain saved detection_boxes.")

    regions = []
    for index, box in enumerate(report_boxes, start=1):
        if not isinstance(box, dict):
            raise PlanError("Each DBNet detection box must be an object.")
        x0, y0, x1, y1 = (box.get(key) for key in ("x0", "y0", "x1", "y1"))
        if any(type(value) is not int for value in (x0, y0, x1, y1)):
            raise PlanError(f"DBNet region {index} has invalid coordinates.")
        roi_box = _validate_box([x0, y0, x1 - x0, y1 - y0], width, height, f"D{index:03d}")
        residual_crop = uncovered_mask.crop((x0, y0, x1, y1))
        histogram = residual_crop.histogram()
        outside_pixels = sum(histogram[1:])
        if outside_pixels == 0:
            continue
        intersecting_ids = []
        for candidate_id, (cx, cy, cw, ch) in ocr_ids:
            if x0 < cx + cw and cx < x1 and y0 < cy + ch and cy < y1:
                intersecting_ids.append(candidate_id)
        regions.append({
            "id": f"D{index:03d}",
            "source_bbox_px": roi_box,
            "detector_box_index": index - 1,
            "raw_mask_pixels_outside_ocr_boxes": outside_pixels,
            "intersecting_ocr_candidate_ids": intersecting_ids,
            "classification": "pending_visual_review",
        })

    output = Path(output_arg).expanduser().resolve()
    overlay_path = (Path(overlay_arg).expanduser().resolve() if overlay_arg
                    else output.with_name("coverage-review-overlay.png"))
    uncovered_path = output.with_name("uncovered-detector-mask.png")
    output.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    uncovered_path.parent.mkdir(parents=True, exist_ok=True)
    uncovered_mask.save(uncovered_path)

    with Image.open(coordinate) as source_image:
        source_image.load()
        overlay = source_image.convert("RGBA")
    draw = ImageDraw.Draw(overlay)
    for candidate_id, (x, y, w, h) in ocr_ids:
        draw.rectangle((x, y, x + w - 1, y + h - 1), outline=(30, 145, 80, 255), width=2)
    for region in regions:
        x, y, w, h = region["source_bbox_px"]
        draw.rectangle((x, y, x + w - 1, y + h - 1), outline=(255, 120, 0, 255), width=2)
        if w >= 32 and h >= 18:
            draw.text((x + 2, y + 2), region["id"], fill=(130, 45, 0, 255), stroke_width=1,
                      stroke_fill=(255, 255, 255, 255))
    overlay.convert("RGB").save(overlay_path)

    result = {
        "schema_version": 1,
        "task": "text_first_detector_coverage_review",
        "source_sha256": source_hash,
        "source_size_px": {"width": width, "height": height},
        "ocr_sha256": sha256_file(ocr_path),
        "detector_report_path": str(detector_path),
        "detector_report_sha256": sha256_file(detector_path),
        "dbnet_raw_mask_path": str(raw_mask_path),
        "dbnet_raw_mask_sha256": sha256_file(raw_mask_path),
        "ocr_candidate_count": len(candidates),
        "detector_box_count": len(report_boxes),
        "uncovered_detector_region_count": len(regions),
        "region_policy": "Saved DBNet boxes with any raw-mask pixels outside OCR boxes are review candidates; they are never classified as text automatically. A full-source visual pass remains required because detector regions can merge artwork and can miss text.",
        "regions": regions,
        "uncovered_mask_path": str(uncovered_path),
        "uncovered_mask_sha256": sha256_file(uncovered_path),
        "review_overlay_path": str(overlay_path),
        "review_overlay_sha256": sha256_file(overlay_path),
        "review_status": "pending_visual_review",
    }
    write_json(output, result)
    return result


def build_whole_image_review(run_dir_arg: str | Path, ocr_arg: str | Path,
                             output_arg: str | Path, overlay_arg: str | Path | None = None) -> dict[str, Any]:
    """Prepare a source-bound whole-image visual coverage review; no detector is implied."""
    run_dir = Path(run_dir_arg).expanduser().resolve()
    manifest = _read_json(run_dir / "manifest.json")
    source = manifest.get("source", {})
    coordinate = Path(source.get("coordinate_path", ""))
    if not coordinate.is_absolute():
        coordinate = run_dir / coordinate
    if not coordinate.is_file() or sha256_file(coordinate) != source.get("coordinate_sha256"):
        raise PlanError("Prepared coordinate image is missing or its hash changed.")
    source_hash = sha256_file(coordinate)
    from selection_gate import SelectionError, require_ready
    try:
        gate = require_ready(run_dir, source_hash)
    except SelectionError as exc:
        raise PlanError(str(exc)) from exc
    width, height = source.get("width_px"), source.get("height_px")
    ocr_path = Path(ocr_arg).expanduser().resolve()
    ocr = _read_json(ocr_path)
    if ocr.get("source", {}).get("sha256") != source_hash:
        raise PlanError("OCR artifact belongs to another coordinate image.")
    if ocr.get("source", {}).get("width_px") != width or ocr.get("source", {}).get("height_px") != height:
        raise PlanError("OCR dimensions do not match the prepared coordinate image.")
    map_ocr_hash = ocr.get("source", {}).get("raw_ocr_sha256") or sha256_file(ocr_path)
    if gate.get("mode") == "partial" and map_ocr_hash != gate.get("ocr_sha256"):
        raise PlanError("OCR artifact changed after the partial-mode numbered selection; show a fresh map before continuing.")
    candidates = ocr.get("candidates")
    if not isinstance(candidates, list):
        raise PlanError("OCR artifact must contain a candidates array.")
    image = Image.open(coordinate).convert("RGBA")
    if image.size != (width, height):
        raise PlanError("Prepared image dimensions do not match manifest.json.")
    draw = ImageDraw.Draw(image)
    seen: set[str] = set()
    for index, item in enumerate(candidates, 1):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise PlanError("Every OCR candidate needs an id before visual coverage review.")
        candidate_id = item["id"]
        if candidate_id in seen:
            raise PlanError(f"Duplicate OCR candidate id: {candidate_id}")
        seen.add(candidate_id)
        x, y, w, h = _validate_box(item.get("box_px"), width, height, candidate_id)
        draw.rectangle((x, y, x + w - 1, y + h - 1), outline=(20, 150, 78, 255), width=2)
        if w >= 20 and h >= 12:
            draw.text((x + 2, y + 1), candidate_id, fill=(0, 90, 45, 255),
                      stroke_width=1, stroke_fill=(255, 255, 255, 230))

    output = Path(output_arg).expanduser().resolve()
    overlay_path = (Path(overlay_arg).expanduser().resolve() if overlay_arg
                    else output.with_name("whole-image-coverage-overlay.png"))
    output.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(overlay_path)
    result = {
        "schema_version": 1,
        "task": "text_first_whole_image_visual_coverage",
        "method": "whole_image_visual_review",
        "source_sha256": source_hash,
        "source_size_px": {"width": width, "height": height},
        "ocr_sha256": sha256_file(ocr_path),
        "ocr_candidate_count": len(candidates),
        "regions": [{"id": "V001", "source_bbox_px": [0, 0, width, height],
                     "scope": "entire coordinate image; human visual review only"}],
        "region_policy": "This is a whole-image visual-review aid, not a text detector or proof that OCR found every text item.",
        "review_overlay_path": str(overlay_path),
        "review_overlay_sha256": sha256_file(overlay_path),
        "review_status": "pending_visual_review",
    }
    write_json(output, result)
    return result


def _validate_box(box: Any, width: int, height: int, candidate_id: str) -> list[int]:
    if not isinstance(box, list) or len(box) != 4 or any(type(n) is not int for n in box):
        raise PlanError(f"{candidate_id}: box_px must be four integer pixel values.")
    x, y, w, h = box
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
        raise PlanError(f"{candidate_id}: box_px is outside the coordinate image.")
    return box


def selection_gate_overlaps(run_dir: Path, box: list[int], target_id: str) -> list[str]:
    from selection_gate import find_target_overlaps
    return find_target_overlaps(run_dir, {"id": target_id, "source_bbox_px": box})


def _validate_source_geometry(value: Any, width: int, height: int, item_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") not in {"quadrilateral", "polygon"}:
        raise PlanError(f"{item_id}: source_geometry must preserve a polygon or quadrilateral.")
    points = value.get("points_px")
    if not isinstance(points, list) or not 3 <= len(points) <= 128:
        raise PlanError(f"{item_id}: source_geometry points_px must contain 3 to 128 points.")
    normalized_points = []
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise PlanError(f"{item_id}: source_geometry has an invalid point.")
        if any(type(coord) not in (int, float) or not math.isfinite(float(coord)) for coord in point):
            raise PlanError(f"{item_id}: source_geometry coordinates must be finite numbers.")
        x, y = float(point[0]), float(point[1])
        if x < 0 or y < 0 or x > width or y > height:
            raise PlanError(f"{item_id}: source_geometry point is outside the coordinate image.")
        normalized_points.append([x, y])
    kind = "quadrilateral" if len(points) == 4 else "polygon"
    if value["kind"] != kind:
        raise PlanError(f"{item_id}: source_geometry kind does not match its full point count.")
    suggestion = value.get("suggested_rotation_deg")
    if suggestion is not None and (type(suggestion) not in (int, float) or not math.isfinite(float(suggestion))):
        raise PlanError(f"{item_id}: suggested source angle must be finite or null.")
    basis = value.get("suggestion_basis")
    if suggestion is not None and (kind != "quadrilateral" or not isinstance(basis, str) or not basis.strip()):
        raise PlanError(f"{item_id}: an angle suggestion needs a quadrilateral basis.")
    return {"kind": kind, "points_px": normalized_points,
            "suggested_rotation_deg": float(suggestion) if suggestion is not None else None,
            "suggestion_basis": basis}


def _decision_map(decisions: dict[str, Any], candidate_ids: set[str]) -> dict[str, dict[str, Any]]:
    assignments = decisions.get("assignments", [])
    if not isinstance(assignments, list):
        raise PlanError("decisions.assignments must be an array.")
    mapped: dict[str, dict[str, Any]] = {}
    for assignment in assignments:
        if not isinstance(assignment, dict):
            raise PlanError("Each batch assignment must be an object.")
        ids = assignment.get("ids")
        category = assignment.get("classification")
        reason = assignment.get("reason")
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
            raise PlanError("Each batch assignment needs a non-empty ids array.")
        if category not in CATEGORIES:
            raise PlanError(f"Invalid classification {category!r}; use {sorted(CATEGORIES)}.")
        if not isinstance(reason, str) or not reason.strip():
            raise PlanError("Every visual assignment needs a non-empty reason.")
        for candidate_id in ids:
            if candidate_id not in candidate_ids:
                raise PlanError(f"Decision references unknown OCR candidate: {candidate_id}")
            if candidate_id in mapped:
                raise PlanError(f"OCR candidate appears in more than one assignment: {candidate_id}")
            mapped[candidate_id] = {
                "classification": category,
                "reason": reason.strip(),
                "reviewer": assignment.get("reviewer", decisions.get("reviewer", "unspecified")),
                "decision_source": assignment.get("decision_source", "visual_override"),
            }
    return mapped


def _group_map(decisions: dict[str, Any], candidate_ids: set[str]) -> dict[str, str]:
    groups = decisions.get("paragraph_groups", [])
    if not isinstance(groups, list):
        raise PlanError("decisions.paragraph_groups must be an array.")
    mapped: dict[str, str] = {}
    for group in groups:
        if not isinstance(group, dict):
            raise PlanError("Each paragraph group override must be an object.")
        group_id = group.get("group_id")
        ids = group.get("ids")
        if not isinstance(group_id, str) or not group_id.strip():
            raise PlanError("Each paragraph group needs a non-empty group_id.")
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
            raise PlanError("Each paragraph group needs a non-empty ids array.")
        for candidate_id in ids:
            if candidate_id not in candidate_ids:
                raise PlanError(f"Paragraph group references unknown OCR candidate: {candidate_id}")
            if candidate_id in mapped:
                raise PlanError(f"OCR candidate appears in more than one paragraph group: {candidate_id}")
            mapped[candidate_id] = group_id.strip()
    return mapped


def build_text_plan(run_dir_arg: str | Path, ocr_arg: str | Path,
                    decisions_arg: str | Path | None = None, *,
                    coverage_arg: str | Path | None = None,
                    require_ready: bool = False) -> dict[str, Any]:
    run_dir = Path(run_dir_arg).expanduser().resolve()
    manifest_path = run_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    source = manifest.get("source", {})
    coordinate = Path(source.get("coordinate_path", ""))
    if not coordinate.is_absolute():
        coordinate = run_dir / coordinate
    if not coordinate.is_file():
        raise PlanError("Prepared coordinate image is missing.")
    actual_source_hash = sha256_file(coordinate)
    if source.get("coordinate_sha256") != actual_source_hash:
        raise PlanError("Prepared coordinate image hash no longer matches manifest.json.")
    from selection_gate import SelectionError, require_ready as require_gate_ready, gate_path
    try:
        gate = require_gate_ready(run_dir, actual_source_hash)
    except SelectionError as exc:
        raise PlanError(str(exc)) from exc

    ocr_path = Path(ocr_arg).expanduser().resolve()
    ocr = _read_json(ocr_path)
    ocr_source = ocr.get("source", {})
    if ocr_source.get("sha256") != actual_source_hash:
        raise PlanError("OCR artifact belongs to another coordinate image; refusing to reuse it.")
    width, height = source.get("width_px"), source.get("height_px")
    if ocr_source.get("width_px") != width or ocr_source.get("height_px") != height:
        raise PlanError("OCR dimensions do not match the prepared coordinate image.")
    ocr_hash = sha256_file(ocr_path)
    is_role_flow = gate.get("workflow_version") == WORKFLOW_VERSION
    raw_ocr_hash = ocr_source.get("raw_ocr_sha256") if is_role_flow else ocr_hash
    if is_role_flow:
        role_meta = ocr.get("text_role_review")
        if not isinstance(role_meta, dict) or role_meta.get("review_status") != "reviewed":
            raise PlanError("2.1.1 workflow requires a completed source-bound text-role inventory.")
        role_path = Path(role_meta.get("path", ""))
        raw_path = Path(role_meta.get("raw_ocr_path", ""))
        if (not role_path.is_file() or sha256_file(role_path) != role_meta.get("sha256")
                or role_meta.get("source_sha256") not in {None, actual_source_hash}
                or not raw_path.is_file() or sha256_file(raw_path) != role_meta.get("raw_ocr_sha256")
                or role_meta.get("raw_ocr_sha256") != raw_ocr_hash):
            raise PlanError("2.1.1 text-role or raw OCR review binding is missing or stale.")
        if role_meta.get("sha256") != gate.get("text_role_review_sha256"):
            raise PlanError("Text-role review does not match the completed review registered for this run.")
        if gate.get("mode") == "partial" and role_meta.get("sha256") != gate.get("text_role_review_sha256"):
            raise PlanError("Text-role inventory changed after partial selection; rebuild numbering and reselect.")
    if gate.get("mode") == "partial" and raw_ocr_hash != gate.get("ocr_sha256"):
        raise PlanError("OCR artifact changed after the partial-mode numbered selection; show a fresh map before continuing.")
    candidates = ocr.get("candidates")
    if not isinstance(candidates, list):
        raise PlanError("OCR artifact must contain a candidates array.")

    normalized: list[dict[str, Any]] = []
    candidate_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise PlanError("Each OCR candidate must be an object.")
        candidate_id = candidate.get("id")
        text = candidate.get("text_candidate")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise PlanError("Every OCR candidate needs a non-empty id.")
        if candidate_id in candidate_ids:
            raise PlanError(f"Duplicate OCR candidate id: {candidate_id}")
        if not isinstance(text, str) or not text.strip():
            raise PlanError(f"{candidate_id}: text_candidate must be non-empty text.")
        candidate_ids.add(candidate_id)
        normalized_item = {
            "id": candidate_id,
            "text": text,
            "source_bbox_px": _validate_box(candidate.get("box_px"), width, height, candidate_id),
            "ocr_block_label": candidate.get("block_label"),
            "paragraph_group_id": f"paragraph-{candidate_id}",
            "paragraph_group_basis": "one OCR candidate block; preserve its internal line breaks",
        }
        for field in ("text_role", "source_ocr_ids", "source_kind", "role_review_reason",
                      "native_representation", "source_location"):
            if field in candidate:
                normalized_item[field] = candidate[field]
        if candidate.get("source_geometry") is not None:
            normalized_item["source_geometry"] = _validate_source_geometry(
                candidate["source_geometry"], width, height, candidate_id)
        normalized.append(normalized_item)

    decision_file_hash = None
    mapped: dict[str, dict[str, Any]] = {}
    group_overrides: dict[str, str] = {}
    decisions: dict[str, Any] = {}
    supplement_items: list[dict[str, Any]] = []
    text_override_count = 0
    if decisions_arg:
        decisions_path = Path(decisions_arg).expanduser().resolve()
        decisions = _read_json(decisions_path)
        if decisions.get("source_sha256") != actual_source_hash:
            raise PlanError("Visual decisions belong to another coordinate image.")
        ocr_hash = sha256_file(ocr_path)
        if decisions.get("ocr_sha256") != ocr_hash:
            raise PlanError("Visual decisions belong to another OCR artifact.")
        text_overrides = decisions.get("ocr_text_overrides", [])
        if not isinstance(text_overrides, list):
            raise PlanError("decisions.ocr_text_overrides must be an array.")
        seen_override_ids: set[str] = set()
        normalized_by_id = {item["id"]: item for item in normalized}
        for override in text_overrides:
            if not isinstance(override, dict):
                raise PlanError("Each OCR text override must be an object.")
            item_id = override.get("candidate_id")
            corrected_text = override.get("text")
            reason = override.get("reason")
            reviewer = override.get("reviewer", decisions.get("reviewer", "unspecified"))
            if item_id not in candidate_ids or item_id in seen_override_ids:
                raise PlanError("OCR text overrides must reference unique existing candidate IDs.")
            if not isinstance(corrected_text, str) or not corrected_text.strip():
                raise PlanError(f"{item_id}: corrected OCR text must be non-empty.")
            if not isinstance(reason, str) or not reason.strip():
                raise PlanError(f"{item_id}: OCR text override needs a visual-review reason.")
            if not isinstance(reviewer, str) or not reviewer.strip():
                raise PlanError(f"{item_id}: OCR text override needs a reviewer.")
            item = normalized_by_id[item_id]
            expected_original = override.get("expected_original_text")
            if expected_original is not None and expected_original != item["text"]:
                raise PlanError(f"{item_id}: original OCR text no longer matches the override record.")
            item["text_override"] = {
                "original_text": item["text"],
                "reason": reason.strip(),
                "reviewer": reviewer.strip(),
                "source_sha256": actual_source_hash,
                "ocr_sha256": ocr_hash,
            }
            item["text"] = corrected_text.strip()
            seen_override_ids.add(item_id)
        text_override_count = len(text_overrides)
        supplements = decisions.get("supplements", [])
        if not isinstance(supplements, list):
            raise PlanError("decisions.supplements must be an array.")
        supplement_items: list[dict[str, Any]] = []
        supplement_ids: set[str] = set()
        for supplement in supplements:
            if not isinstance(supplement, dict):
                raise PlanError("Each visual supplement must be an object.")
            item_id = supplement.get("id")
            item_text = supplement.get("text")
            if not isinstance(item_id, str) or not item_id or item_id in candidate_ids or item_id in supplement_ids:
                raise PlanError("Supplement IDs must be unique and distinct from OCR candidate IDs.")
            if not isinstance(item_text, str) or not item_text.strip():
                raise PlanError(f"{item_id}: supplement text must be non-empty.")
            group_id = supplement.get("paragraph_group_id")
            if not isinstance(group_id, str) or not group_id.strip():
                raise PlanError(f"{item_id}: supplement needs a paragraph_group_id.")
            supplement_ids.add(item_id)
            supplement_item = {
                "id": item_id,
                "text": item_text,
                "source_bbox_px": _validate_box(supplement.get("source_bbox_px"), width, height, item_id),
                "ocr_block_label": "manual_visual_supplement",
                "paragraph_group_id": group_id.strip(),
                "paragraph_group_basis": supplement.get("paragraph_group_basis", "batch visual supplement"),
                "coverage_region_ids": supplement.get("coverage_region_ids", []),
            }
            if supplement.get("source_geometry") is not None:
                supplement_item["source_geometry"] = _validate_source_geometry(
                    supplement["source_geometry"], width, height, item_id)
            supplement_items.append(supplement_item)
        all_candidate_ids = candidate_ids | supplement_ids
        mapped = _decision_map(decisions, all_candidate_ids)
        group_overrides = _group_map(decisions, candidate_ids)
        decision_file_hash = sha256_file(decisions_path)

    coverage_artifact = None
    coverage_artifact_hash = None
    coverage_method = "none"
    coverage_review = {}
    coverage_review_status = "pending_visual_review"
    coverage_review_reason = "Generate the source-bound whole-image review overlay and inspect the entire source."
    coverage_ready = False
    expected_region_ids: set[str] = set()
    if coverage_arg:
        coverage_path = Path(coverage_arg).expanduser().resolve()
        coverage_artifact = _read_json(coverage_path)
        if coverage_artifact.get("source_sha256") != actual_source_hash:
            raise PlanError("Coverage artifact belongs to another coordinate image.")
        if coverage_artifact.get("ocr_sha256") != sha256_file(ocr_path):
            raise PlanError("Coverage artifact belongs to another OCR artifact.")
        if coverage_artifact.get("source_size_px") != {"width": width, "height": height}:
            raise PlanError("Coverage artifact dimensions do not match the coordinate image.")
        coverage_method = coverage_artifact.get("method", "detector_coverage")
        if coverage_method == "whole_image_visual_review":
            if coverage_artifact.get("task") != "text_first_whole_image_visual_coverage":
                raise PlanError("Whole-image review artifact has an unsupported task type.")
            evidence_pairs = (("review_overlay_path", "review_overlay_sha256"),)
            review_key = "whole_image_visual_review"
            default_reason = "Inspect the original and OCR overlay across the entire source; record any missed text."
        else:
            detector_path = Path(coverage_artifact.get("detector_report_path", ""))
            mask_path = Path(coverage_artifact.get("dbnet_raw_mask_path", ""))
            if not detector_path.is_file() or sha256_file(detector_path) != coverage_artifact.get("detector_report_sha256"):
                raise PlanError("Detector report no longer matches the coverage artifact.")
            if not mask_path.is_file() or sha256_file(mask_path) != coverage_artifact.get("dbnet_raw_mask_sha256"):
                raise PlanError("DBNet mask no longer matches the coverage artifact.")
            evidence_pairs = (("uncovered_mask_path", "uncovered_mask_sha256"),
                              ("review_overlay_path", "review_overlay_sha256"))
            review_key = "detector_coverage_review"
            default_reason = "Review detector regions against the source and inspect the entire source for omissions."
        for path_key, hash_key in evidence_pairs:
            evidence_path = Path(coverage_artifact.get(path_key, ""))
            if not evidence_path.is_file() or sha256_file(evidence_path) != coverage_artifact.get(hash_key):
                raise PlanError(f"Coverage visual evidence {path_key} is missing or changed.")
        regions = coverage_artifact.get("regions")
        if not isinstance(regions, list):
            raise PlanError("Coverage artifact must contain a regions array.")
        expected_region_ids = {region.get("id") for region in regions if isinstance(region, dict)}
        if len(expected_region_ids) != len(regions) or not all(isinstance(item, str) for item in expected_region_ids):
            raise PlanError("Coverage artifact contains invalid or duplicate region IDs.")
        if coverage_method == "whole_image_visual_review":
            if len(regions) != 1 or regions[0].get("id") != "V001" or regions[0].get("source_bbox_px") != [0, 0, width, height]:
                raise PlanError("Whole-image visual review must bind its single full-canvas region V001.")
        coverage_artifact_hash = sha256_file(coverage_path)
        coverage_review = decisions.get(review_key, {})
        if not isinstance(coverage_review, dict):
            raise PlanError(f"{review_key} must be an object.")
        coverage_review_status = coverage_review.get("status", "pending_visual_review")
        coverage_review_reason = coverage_review.get("reason", default_reason)
        if coverage_review_status not in {"pending_visual_review", "confirmed"}:
            raise PlanError(f"{review_key}.status must be pending_visual_review or confirmed.")
        reviewer = coverage_review.get("reviewer")
        reviewed_ids = coverage_review.get("reviewed_region_ids", [])
        region_coverage_hash_ok = coverage_review.get("coverage_artifact_sha256") == coverage_artifact_hash
        whole_source_reviewed = coverage_review.get("whole_source_visual_review") is True
        regions_reviewed = (
            isinstance(reviewed_ids, list)
            and all(isinstance(item, str) for item in reviewed_ids)
            and len(reviewed_ids) == len(set(reviewed_ids))
            and set(reviewed_ids) == expected_region_ids
        )
        region_batches = coverage_review.get("region_review_batches", [])
        batch_region_ids: list[str] = []
        valid_batches = isinstance(region_batches, list) and bool(region_batches)
        if valid_batches:
            for batch in region_batches:
                if not isinstance(batch, dict):
                    valid_batches = False
                    break
                ids = batch.get("region_ids")
                reason = batch.get("reason")
                if (not isinstance(ids, list) or not ids
                        or not all(isinstance(item, str) for item in ids)
                        or batch.get("outcome") != "checked_against_source"
                        or not isinstance(reason, str) or not reason.strip()):
                    valid_batches = False
                    break
                batch_region_ids.extend(ids)
            valid_batches = (
                valid_batches
                and len(batch_region_ids) == len(set(batch_region_ids))
                and set(batch_region_ids) == expected_region_ids
            )
        supplement_coverage_ok = all(
            isinstance(item.get("coverage_region_ids"), list)
            and item["coverage_region_ids"]
            and all(isinstance(region_id, str) for region_id in item["coverage_region_ids"])
            and set(item["coverage_region_ids"]).issubset(expected_region_ids)
            for item in supplement_items
        )
        supplement_classification_ok = all(
            item["id"] in mapped and mapped[item["id"]]["classification"] in CATEGORIES
            for item in supplement_items
        )
        coverage_ready = (
            coverage_review_status == "confirmed"
            and isinstance(reviewer, str) and bool(reviewer.strip())
            and isinstance(coverage_review_reason, str) and bool(coverage_review_reason.strip())
            and region_coverage_hash_ok and whole_source_reviewed and regions_reviewed and valid_batches
            and supplement_coverage_ok and supplement_classification_ok
        )
    coverage = decisions.get("ocr_coverage_review", {})
    if not isinstance(coverage, dict):
        raise PlanError("ocr_coverage_review must be an object.")
    coverage_status = coverage.get("status", "pending_visual_review")
    if coverage_status not in {"pending_visual_review", "confirmed"}:
        raise PlanError("ocr_coverage_review.status must be pending_visual_review or confirmed.")
    coverage_reason = coverage.get("reason")
    if coverage_status == "confirmed" and (not isinstance(coverage_reason, str) or not coverage_reason.strip()):
        raise PlanError("A confirmed OCR coverage review needs a reason.")
    if not isinstance(coverage_reason, str) or not coverage_reason.strip():
        coverage_reason = "Inspect the whole image for OCR omissions before Stage 2."

    items = []
    for item in normalized:
        if item["id"] in group_overrides:
            item["paragraph_group_id"] = group_overrides[item["id"]]
            item["paragraph_group_basis"] = "batch visual grouping override"
        decision = mapped.get(item["id"])
        if decision:
            item.update({
                "classification": decision["classification"],
                "classification_status": "visually_reviewed",
                "classification_basis": decision["decision_source"],
                "classification_reason": decision["reason"],
                "reviewer": decision["reviewer"],
            })
        else:
            item.update({
                "classification": "editable",
                "classification_status": "pending_visual_review",
                "classification_basis": "workflow_default_only",
                "classification_reason": (
                    "Proposed as editable poster copy by default. OCR cannot determine whether this text "
                    "belongs inside an illustration or whether a title can be reproduced as native text."
                ),
                "reviewer": None,
            })
        items.append(item)

    for item in supplement_items:
        decision = mapped.get(item["id"])
        if decision:
            item.update({
                "classification": decision["classification"],
                "classification_status": "visually_reviewed",
                "classification_basis": "coverage_batch_visual_supplement",
                "classification_reason": decision["reason"],
                "reviewer": decision["reviewer"],
            })
        else:
            item.update({
                "classification": "editable",
                "classification_status": "pending_visual_review",
                "classification_basis": "workflow_default_only",
                "classification_reason": "A detector miss was transcribed, but still needs an explicit visual class.",
                "reviewer": None,
            })
        items.append(item)

    workflow = {"version": gate.get("workflow_version", "2.1.0"), "mode": gate.get("mode"), "status": "ready",
                "selection_required": True,
                "selection_gate_sha256": sha256_file(gate_path(run_dir)) if gate.get("schema_version") == 1 else None}
    partial_selection = None
    if gate.get("mode") == "partial":
        if supplement_items:
            raise PlanError("Partial selection cannot import uncoupled decision supplements; add each missed target through the screenshot-bound selection gate.")
        map_path = Path(gate.get("number_map_path", ""))
        if not map_path.is_file() or sha256_file(map_path) != gate.get("number_map_sha256"):
            raise PlanError("Displayed OCR number map is stale or missing; build and show a fresh numbered overlay.")
        number_map = _read_json(map_path)
        if (number_map.get("source_sha256") != actual_source_hash
                or number_map.get("ocr_sha256") != raw_ocr_hash
                or number_map.get("preview_sha256") != gate.get("numbered_overlay_sha256")):
            raise PlanError("Displayed OCR number map no longer matches this source, OCR artifact, or overlay.")
        map_items = number_map.get("items")
        numberable_ids = ({row["id"] for row in normalized if row.get("text_role") == "main_copy"}
                          if is_role_flow else candidate_ids)
        if not isinstance(map_items, list) or len(map_items) != len(numberable_ids):
            raise PlanError("Displayed number map does not cover exactly the reviewed main-copy candidates.")
        map_by_id = {row.get("ocr_id"): row for row in map_items if isinstance(row, dict)}
        normalized_by_id = {row["id"]: row for row in normalized}
        if set(map_by_id) != numberable_ids:
            raise PlanError("Reviewed main-copy IDs changed after the numbered map was shown.")
        if is_role_flow and number_map.get("role_review_sha256") != ocr.get("text_role_review", {}).get("sha256"):
            raise PlanError("Displayed number map belongs to another source text-role review.")
        for item_id, row in map_by_id.items():
            current = normalized_by_id[item_id]
            if row.get("source_bbox_px") != current.get("source_bbox_px"):
                raise PlanError(f"{item_id}: OCR source box changed after the numbered map; show a fresh map.")
        if gate.get("partial_method") == "ocr_numbers":
            selected_ocr_ids = gate.get("selected_ids")
            if not isinstance(selected_ocr_ids, list) or not selected_ocr_ids:
                raise PlanError("Partial OCR selection has no selected IDs; it cannot default to full mode.")
            if not set(selected_ocr_ids).issubset(numberable_ids):
                raise PlanError("Selected IDs are stale or are not reviewed main-copy items.")
            selected_numbers = gate.get("selected_numbers")
            if (not isinstance(selected_numbers, list) or len(selected_numbers) != len(selected_ocr_ids)
                    or any(type(n) is not int or n < 1 or n > len(map_items) for n in selected_numbers)):
                raise PlanError("Selected numbers are invalid or no longer match the displayed map.")
            number_to_id = {row.get("number"): row.get("ocr_id") for row in map_items}
            if [number_to_id.get(n) for n in selected_numbers] != selected_ocr_ids:
                raise PlanError("Selected number-to-OCR-ID mapping changed; show a fresh map and reselect.")
            screenshot_targets = []
        elif gate.get("partial_method") == "unreadable_screenshot":
            selected_ocr_ids = []
            screenshot_targets = gate.get("screenshot_targets", [])
            if not screenshot_targets:
                raise PlanError("Screenshot selection is missing its target record.")
            expected_screenshot_selection = [target.get("duplicate_of_inventory_id") or target.get("id")
                                             for target in screenshot_targets]
            if gate.get("selected_ids") != expected_screenshot_selection:
                raise PlanError("Selected screenshot IDs do not match the saved screenshot targets.")
            for target in screenshot_targets:
                if target.get("status") != "confirmed" or target.get("source_sha256") != actual_source_hash:
                    raise PlanError(f"{target.get('id')}: screenshot location or source binding is unresolved.")
                target_path = Path(target.get("evidence_path", ""))
                if not target_path.is_file() or sha256_file(target_path) != target.get("screenshot_sha256"):
                    raise PlanError(f"{target.get('id')}: screenshot evidence is stale or missing.")
                box = _validate_box(target.get("source_bbox_px"), width, height, target["id"])
                overlaps = selection_gate_overlaps(run_dir, box, target.get("id"))
                unrecorded = set(overlaps) - set(target.get("overlap_candidate_ids", []))
                if unrecorded:
                    raise PlanError(f"{target['id']}: source box overlaps OCR IDs {sorted(unrecorded)}; confirm exact text ownership before continuing.")
                if overlaps and target.get("overlap_resolution") not in {
                        "user_confirmed_target_is_distinct_from_preserved_ocr_text",
                        "screenshot_matches_reviewed_main_copy"}:
                    raise PlanError(f"{target['id']}: source box overlaps OCR IDs {overlaps}; text ownership is unresolved.")
                duplicate_id = target.get("duplicate_of_inventory_id")
                conflicting_selected = set(overlaps).intersection(gate.get("selected_ids", [])) - ({duplicate_id} if duplicate_id else set())
                if conflicting_selected:
                    raise PlanError(f"{target['id']}: screenshot target duplicates a selected OCR block.")
                if duplicate_id:
                    continue
                items.append({"id": target["id"], "text": target["text"], "source_bbox_px": box,
                              "ocr_block_label": "user_screenshot_transcription",
                              "paragraph_group_id": target["id"],
                              "paragraph_group_basis": "source-mapped screenshot target",
                              "text_role": "main_copy", "source_kind": "screenshot_selection",
                              "classification": "editable", "classification_status": "visually_reviewed",
                              "classification_basis": "explicit partial-mode screenshot selection",
                              "classification_reason": "User-provided screenshot was mapped to this exact source region.",
                              "reviewer": target.get("confirmed_by"),
                              "selection_evidence": {"screenshot_sha256": target["screenshot_sha256"],
                                                     "source_bbox_px": box,
                                                     "overlap_candidate_ids": overlaps,
                                                     "overlap_resolution": target.get("overlap_resolution")}})
        else:
            raise PlanError("Partial workflow method is missing; numbers or screenshot must be explicitly selected.")

        screenshot_selection_ids = {target.get("duplicate_of_inventory_id") or target.get("id")
                                    for target in screenshot_targets}
        selected_ids = set(selected_ocr_ids) | screenshot_selection_ids
        if len(selected_ids) != len(selected_ocr_ids) + len(screenshot_targets):
            raise PlanError("Partial selection contains duplicate OCR or screenshot IDs.")
        for item in items:
            item_id = item["id"]
            is_selected = item_id in selected_ids
            if item_id in candidate_ids:
                selected_class = (item.get("native_representation", "editable")
                                  if is_role_flow and is_selected else "editable")
                item.update({
                    "classification": selected_class if is_selected else "background_internal",
                    "classification_status": "visually_reviewed",
                    "classification_basis": "explicit partial-mode scope selection",
                    "classification_reason": ("User selected this numbered OCR block for an editable Type layer."
                                               if is_selected else
                                               "User selected only specified OCR blocks; this text remains in the source background."),
                    "reviewer": "user selection",
                    "selection_role": "editable_selected" if is_selected else "preserve_in_background",
                })
            elif item_id not in selected_ids:
                item.update({"classification": "background_internal", "classification_status": "visually_reviewed",
                             "classification_basis": "partial-mode scope default",
                             "classification_reason": "Not explicitly selected for conversion; preserve with the source background.",
                             "reviewer": "user selection", "selection_role": "preserve_in_background"})
        preserved_ocr_ids = sorted(candidate_ids - selected_ids)
        partial_selection = {
            "schema_version": 1,
            "status": "ready",
            "mode": "partial",
            "method": gate.get("partial_method"),
            "source_sha256": actual_source_hash,
            "ocr_sha256": ocr_hash,
            "number_map_sha256": gate.get("number_map_sha256"),
            "numbered_overlay_sha256": gate.get("numbered_overlay_sha256"),
            "selected_numbers": gate.get("selected_numbers", []),
            "selected_ocr_ids": selected_ocr_ids,
            "selected_inventory_ids": selected_ocr_ids,
            "selected_screenshot_targets": screenshot_targets,
            "selected_ids": sorted(selected_ids),
            "preserved_ocr_ids": preserved_ocr_ids,
            "preserved_inventory_ids": preserved_ocr_ids,
            "role_review_sha256": ocr.get("text_role_review", {}).get("sha256") if is_role_flow else None,
            "raw_ocr_sha256": raw_ocr_hash,
            "scope_contract": "Only selected main-copy items may become editable Type layers; every preserved inventory item remains visible in the full-canvas background.",
        }
        workflow["selection_gate_sha256"] = sha256_file(gate_path(run_dir))

    pending_items = sum(item["classification_status"] == "pending_visual_review" for item in items)
    ready = pending_items == 0 and coverage_ready
    blockers = []
    if pending_items:
        blockers.append("pending_text_classification")
    if not coverage_arg:
        blockers.append("missing_whole_image_coverage_review")
    elif not coverage_ready:
        blockers.append("whole_image_coverage_review_or_supplements_incomplete")
    plan = {
        "schema_version": 1,
        "task": "text_first_plan",
        "workflow": workflow,
        "partial_selection": partial_selection,
        "source": {
            "coordinate_path": str(coordinate),
            "coordinate_sha256": actual_source_hash,
            "original_sha256": source["original_sha256"],
            "width_px": width,
            "height_px": height,
            "coordinate_space": "source.png pixels; bbox is [x, y, width, height]",
        },
        "ocr": {
            "artifact_path": str(ocr_path),
            "artifact_sha256": sha256_file(ocr_path),
            "raw_ocr_sha256": raw_ocr_hash,
            "backend": ocr.get("backend", "unknown"),
            "advisory_only": False if ocr.get("backend") == "manual" else bool(ocr.get("advisory_only", True)),
            "candidate_count": len(normalized),
            "manual_supplement_count": len(supplement_items),
            "manual_screenshot_selection_count": len(gate.get("screenshot_targets", [])) if gate.get("mode") == "partial" else 0,
            "text_override_count": text_override_count,
            "text_overrides_sha256": decision_file_hash if text_override_count else None,
        },
        "visual_coverage": {
            "method": coverage_method,
            "artifact_path": str(Path(coverage_arg).expanduser().resolve()) if coverage_arg else None,
            "artifact_sha256": coverage_artifact_hash,
            "detector_report_sha256": coverage_artifact.get("detector_report_sha256") if coverage_artifact else None,
            "dbnet_raw_mask_sha256": coverage_artifact.get("dbnet_raw_mask_sha256") if coverage_artifact else None,
            "region_count": len(expected_region_ids),
            "reviewed_region_count": len(coverage_review.get("reviewed_region_ids", [])) if isinstance(coverage_review.get("reviewed_region_ids", []), list) else 0,
            "status": "confirmed" if coverage_ready else "pending_visual_review",
            "whole_source_visual_review": coverage_review.get("whole_source_visual_review") is True,
            "reason": coverage_review_reason,
        },
        "classification_contract": {
            "allowed_values": sorted(CATEGORIES),
            "unassigned_policy": "pending_visual_review; never treat workflow default as confirmed",
            "batch_decisions_sha256": decision_file_hash,
        },
        "review": {
            "classified_count": len(items) - pending_items,
            "pending_classification_count": pending_items,
            "ocr_coverage_status": coverage_status,
            "ocr_coverage_reason": coverage_reason,
            "visual_coverage_status": "confirmed" if coverage_ready else "pending_visual_review",
            "visual_coverage_method": coverage_method,
            "visual_coverage_reason": coverage_review_reason,
            "ready_for_stage2": ready,
            "blockers": blockers,
        },
        "items": items,
        "text_role_inventory": ocr.get("text_role_review") if is_role_flow else None,
        "notes": [
            "OCR is advisory; verify every candidate's wording and source box against the source image.",
            "OCR cannot determine whether text is part of an illustration; visual classification is explicit.",
            "This stage does not remove text from the background, extract title pixels, or build a PSD.",
        ],
    }
    if require_ready and not ready:
        raise PlanError("Plan is not ready for Stage 2: " + ", ".join(plan["review"]["blockers"]))
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="copy one image and bind its source hashes")
    prepare.add_argument("image")
    prepare.add_argument("--out-root", required=True)
    coverage = sub.add_parser("coverage", help="compare optional saved DBNet regions with OCR boxes")
    coverage.add_argument("--run-dir", required=True)
    coverage.add_argument("--ocr-candidates", required=True)
    coverage.add_argument("--detector-report", required=True)
    coverage.add_argument("--output", required=True)
    coverage.add_argument("--overlay", help="defaults beside --output")
    visual = sub.add_parser("coverage-review", help="create a source-bound whole-image visual-review overlay (no detector)")
    visual.add_argument("--run-dir", required=True)
    visual.add_argument("--ocr-candidates", required=True)
    visual.add_argument("--output", required=True)
    visual.add_argument("--overlay", help="defaults beside --output")
    plan = sub.add_parser("plan", help="convert a saved OCR candidate artifact into a text plan")
    plan.add_argument("--run-dir", required=True)
    plan.add_argument("--ocr-candidates", required=True)
    plan.add_argument("--decisions", help="optional batch visual decisions JSON")
    plan.add_argument("--coverage-review", "--coverage-candidates", dest="coverage_candidates",
                      help="source-bound visual-review artifact; --coverage-candidates is retained for older detector evidence")
    plan.add_argument("--output", help="defaults to <run-dir>/text-plan.json")
    plan.add_argument("--require-ready", action="store_true", help="fail if any classification or OCR coverage is pending")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            print(prepare_image(args.image, args.out_root))
            return 0
        if args.command == "coverage":
            result = build_coverage_candidates(
                args.run_dir, args.ocr_candidates, args.detector_report, args.output, args.overlay
            )
            print(json.dumps({
                "status": result["review_status"],
                "output": str(Path(args.output).expanduser().resolve()),
                "regions": result["uncovered_detector_region_count"],
                "overlay": result["review_overlay_path"],
            }, ensure_ascii=False))
            return 0
        if args.command == "coverage-review":
            result = build_whole_image_review(args.run_dir, args.ocr_candidates, args.output, args.overlay)
            print(json.dumps({"status": result["review_status"], "method": result["method"],
                              "output": str(Path(args.output).expanduser().resolve()),
                              "overlay": result["review_overlay_path"]}, ensure_ascii=False))
            return 0
        result = build_text_plan(
            args.run_dir, args.ocr_candidates, args.decisions,
            coverage_arg=args.coverage_candidates, require_ready=args.require_ready
        )
        output = Path(args.output).expanduser().resolve() if args.output else Path(args.run_dir).expanduser().resolve() / "text-plan.json"
        write_json(output, result)
        print(json.dumps({"status": "ok", "output": str(output), "items": len(result["items"]),
                          "pending_classification": result["review"]["pending_classification_count"],
                          "ready_for_stage2": result["review"]["ready_for_stage2"]}, ensure_ascii=False))
        return 0
    except (PlanError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
