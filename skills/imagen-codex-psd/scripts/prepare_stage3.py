#!/usr/bin/env python3
"""Bind reviewed Stage 2 inputs and a style fixture to the TySh writer contract."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
from pathlib import Path
from typing import Any

WEIGHTS = {"ExtraLight", "Light", "Normal", "Regular", "Medium", "Bold", "Heavy"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class Stage3InputError(ValueError):
    pass


def _style_index(style_records: list[Any], editable_ids: set[str]) -> dict[str, dict[str, Any]]:
    style_by_id: dict[str, dict[str, Any]] = {}
    for style in style_records:
        text_id = style.get("text_item_id") if isinstance(style, dict) else None
        if not isinstance(text_id, str) or text_id in style_by_id:
            raise Stage3InputError("Style plan has an invalid or duplicate text_item_id")
        style_by_id[text_id] = style
    if set(style_by_id) != editable_ids:
        missing = sorted(editable_ids - set(style_by_id))
        unexpected = sorted(set(style_by_id) - editable_ids)
        raise Stage3InputError(
            f"Style plan IDs must exactly cover selected editable items; missing={missing}, unexpected={unexpected}"
        )
    return style_by_id


def _validated_style_runs(style: dict[str, Any], display_text: str, text_id: str,
                          inherited_tracking: int) -> list[dict[str, Any]] | None:
    raw_runs = style.get("style_runs")
    if raw_runs is None:
        return None
    if not isinstance(raw_runs, list) or not raw_runs:
        raise Stage3InputError(f"{text_id}: style_runs must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    for index, run in enumerate(raw_runs):
        label = f"{text_id} style_runs[{index}]"
        if not isinstance(run, dict) or not isinstance(run.get("text"), str) or not run["text"] or "\x00" in run["text"]:
            raise Stage3InputError(f"{label}: text must be a non-empty string without NUL")
        weight = run.get("font_weight")
        size = run.get("font_size_pt")
        color = run.get("color_rgba")
        if weight not in WEIGHTS or type(size) not in (int, float) or not math.isfinite(float(size)) or size <= 0:
            raise Stage3InputError(f"{label}: font_weight and positive finite font_size_pt are required")
        if (not isinstance(color, list) or len(color) not in (3, 4)
                or any(type(channel) is not int or channel < 0 or channel > 255 for channel in color)):
            raise Stage3InputError(f"{label}: color_rgba must contain 3 or 4 integers in 0..255")
        tracking = run.get("tracking", inherited_tracking)
        if type(tracking) is not int:
            raise Stage3InputError(f"{label}: tracking must be an integer")
        normalized.append({
            "text": run["text"], "font_weight": weight, "font_size_pt": float(size),
            "color_rgba": color + [255] if len(color) == 3 else color,
            "tracking": tracking,
        })
    if "".join(run["text"] for run in normalized) != display_text:
        raise Stage3InputError(
            f"{text_id}: style_runs must cover display text exactly once, in order, including line breaks"
        )
    return normalized


def _validated_vertical_alignment(style: dict[str, Any], text_id: str) -> str:
    alignment = style.get("vertical_alignment", "top")
    if alignment != "top":
        raise Stage3InputError(
            f"{text_id}: vertical_alignment={alignment!r} is unsupported; use top until Photoshop reflow is validated"
        )
    return alignment


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Stage3InputError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise Stage3InputError(f"JSON root must be an object: {path}")
    return result


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_reference(manifest: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise Stage3InputError(f"{label} path is missing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = manifest.parent / path
    path = path.resolve()
    if not path.is_file():
        raise Stage3InputError(f"{label} file is missing: {path}")
    return path


def box(value: Any, canvas: tuple[int, int], label: str) -> list[int]:
    if not isinstance(value, list) or len(value) != 4 or any(type(x) is not int for x in value):
        raise Stage3InputError(f"{label} must be integer [x,y,width,height]")
    x, y, width, height = value
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > canvas[0] or y + height > canvas[1]:
        raise Stage3InputError(f"{label} is outside the source canvas")
    return value


def point(value: Any, canvas: tuple[int, int], label: str) -> list[float]:
    width, height = canvas
    if (not isinstance(value, list) or len(value) != 2
            or any(type(v) not in (int, float) or not math.isfinite(float(v)) for v in value)):
        raise Stage3InputError(f"{label} must be a finite [x, y] point")
    x, y = float(value[0]), float(value[1])
    if x < 0 or y < 0 or x > width or y > height:
        raise Stage3InputError(f"{label} must be inside the source canvas")
    return [x, y]


def _asset_record(record: dict[str, Any], manifest_path: Path, root: Path, canvas: tuple[int, int],
                  source_hash: str, plan_hash: str, base_hash: str, lineage_kind: str) -> dict[str, Any]:
    asset_id = record.get("visual_unit_id") or record.get("id")
    if not isinstance(asset_id, str) or not SAFE_ID.fullmatch(asset_id):
        raise Stage3InputError("Foreground asset ID is invalid")
    source_box = box(record.get("source_box_px") or record.get("source_bbox_px"), canvas, f"asset {asset_id} source box")
    asset_path = resolve_reference(manifest_path, record.get("asset_path") or record.get("alpha_asset_path"),
                                   f"asset {asset_id}")
    asset_hash = sha256_file(asset_path)
    declared_hash = record.get("asset_sha256") or record.get("alpha_asset_sha256")
    if asset_hash != declared_hash:
        raise Stage3InputError(f"Asset {asset_id} hash mismatch")
    output_path = root / "assets" / f"{asset_id}.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(asset_path, output_path)
    return {
        "visual_unit_id": asset_id,
        "source_box_px": source_box,
        "asset_path": str(output_path.relative_to(root)),
        "asset_sha256": asset_hash,
        "native_text_extraction_allowed": False,
        "internal_text_retained": False,
        "asset_input": {"kind": lineage_kind, "text_free_base": False,
                        "manifest_path": str(manifest_path), "manifest_sha256": sha256_file(manifest_path),
                        "source_sha256": source_hash, "text_plan_sha256": plan_hash,
                        "clean_base_sha256": base_hash},
    }


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    source_path = Path(args.source).expanduser().resolve()
    plan_path = Path(args.text_plan).expanduser().resolve()
    background_path = Path(args.background_build).expanduser().resolve()
    style_path = Path(args.style_plan).expanduser().resolve()
    title_paths = [Path(value).expanduser().resolve() for value in args.title_reviews]
    for path in (source_path, plan_path, background_path, style_path, *title_paths):
        if not path.is_file():
            raise Stage3InputError(f"Required input does not exist: {path}")
    plan = read_json(plan_path)
    source_hash, plan_hash = sha256_file(source_path), sha256_file(plan_path)
    source_meta = plan.get("source", {})
    if source_meta.get("coordinate_sha256") != source_hash:
        raise Stage3InputError("Text-plan source hash does not match the supplied coordinate source")
    workflow_state = None
    coordinate_ref = Path(source_meta.get("coordinate_path", "")).expanduser()
    if not coordinate_ref.is_absolute():
        coordinate_ref = plan_path.parent / coordinate_ref
    run_dir = coordinate_ref.resolve().parent
    run_manifest_path = run_dir / "manifest.json"
    if run_manifest_path.is_file():
        run_manifest = read_json(run_manifest_path)
        run_workflow = run_manifest.get("workflow", {})
        if (run_workflow.get("selection_required") is True
                or run_workflow.get("version") in {"2.1.0", "2.1.1"}):
            from selection_gate import SelectionError, gate_path, require_ready
            try:
                workflow_state = require_ready(run_dir, source_hash)
            except SelectionError as exc:
                raise Stage3InputError(str(exc)) from exc
            workflow = plan.get("workflow")
            if (not isinstance(workflow, dict) or workflow.get("version") != workflow_state.get("workflow_version")
                    or workflow.get("mode") != workflow_state.get("mode")
                    or workflow.get("selection_gate_sha256") != sha256_file(gate_path(run_dir))):
                raise Stage3InputError("Text plan does not match the current explicit selection gate")
    width, height = source_meta.get("width_px"), source_meta.get("height_px")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise Stage3InputError("Text-plan canvas size is invalid")
    canvas = (width, height)
    items = plan.get("items")
    if not isinstance(items, list):
        raise Stage3InputError("Text plan has no items array")
    by_id = {item["id"]: item for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)}
    if len(by_id) != len(items):
        raise Stage3InputError("Text plan has invalid or duplicate item IDs")

    background = read_json(background_path)
    if background.get("source_sha256") != source_hash or background.get("text_plan_sha256") != plan_hash:
        raise Stage3InputError("Background build source/text-plan binding is stale")
    if background.get("background_ready") is not True or background.get("ready_for_psd") is not False:
        raise Stage3InputError("Background import is not accepted or PSD readiness was misreported")
    if workflow_state and background.get("partial_selection") != plan.get("partial_selection"):
        raise Stage3InputError("Background build does not preserve the plan's selected/preserved scope record")
    if workflow_state and workflow_state.get("workflow_version") == "2.1.1":
        role_inventory = plan.get("text_role_inventory")
        if (not isinstance(role_inventory, dict) or role_inventory.get("review_status") != "reviewed"
                or role_inventory.get("sha256") != workflow_state.get("text_role_review_sha256")
                or role_inventory.get("path") != workflow_state.get("text_role_review_path")
                or background.get("text_role_review_sha256") != role_inventory.get("sha256")):
            raise Stage3InputError("Stage 3 requires the same reviewed source text inventory as background acceptance")
        role_path = Path(role_inventory["path"])
        if not role_path.is_file() or sha256_file(role_path) != role_inventory.get("sha256"):
            raise Stage3InputError("Stage 3 source text-role inventory is missing or changed")
        role_doc = read_json(role_path)
        ledger_items = {item.get("id"): item for item in role_doc.get("items", []) if isinstance(item, dict)}
        for item_id in role_inventory.get("item_ids", []):
            source_item, plan_item = ledger_items.get(item_id), by_id.get(item_id)
            if (not isinstance(source_item, dict) or not isinstance(plan_item, dict)
                    or plan_item.get("text") != source_item.get("text")
                    or plan_item.get("source_bbox_px") != source_item.get("source_bbox_px")
                    or plan_item.get("text_role") != source_item.get("text_role")
                    or plan_item.get("source_location") != source_item.get("source_location")):
                raise Stage3InputError(f"Stage 3 text item {item_id} differs from its reviewed source ledger")
        if isinstance(plan.get("partial_selection"), dict):
            review_record = background.get("background_review")
            if not isinstance(review_record, dict) or review_record.get("status") != "accepted":
                raise Stage3InputError("Stage 3 has no accepted item-level background review")
            review_path = Path(review_record.get("path", "")).expanduser().resolve()
            if not review_path.is_file() or sha256_file(review_path) != review_record.get("sha256"):
                raise Stage3InputError("Stage 3 accepted background review is missing or changed")
            review_doc = read_json(review_path)
            try:
                from text_background import BackgroundError, _validate_accepted_review
                _validate_accepted_review(review_doc, items, plan)
            except BackgroundError as exc:
                raise Stage3InputError(f"Stage 3 background review does not prove complete text retention: {exc}") from exc
    expected_title_ids = {item["id"] for item in items if item["classification"] == "title_raster"}
    if set(background.get("pending_titles", [])) != expected_title_ids:
        raise Stage3InputError("Background pending title IDs do not match the text plan")
    base_record = background.get("background")
    if not isinstance(base_record, dict) or base_record.get("pixel_identical_to_clean_base") is not True:
        raise Stage3InputError("Background build does not bind an imported clean base")
    base_path = Path(base_record.get("path", "")).expanduser().resolve()
    if not base_path.is_file():
        raise Stage3InputError("Imported clean-base PNG is missing")
    base_hash = sha256_file(base_path)
    if base_hash != background.get("clean_base_sha256") or base_hash != base_record.get("sha256"):
        raise Stage3InputError("Imported clean-base hash mismatch")
    exception_state = background.get("foreground_exceptions")
    if not isinstance(exception_state, dict):
        raise Stage3InputError("Background build has no foreground-exception state")
    expected_ids = exception_state.get("expected_missing_ids")
    included_ids = exception_state.get("included_ids")
    pending_ids = exception_state.get("pending_ids")
    if any(not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value)
           for value in (expected_ids, included_ids, pending_ids)):
        raise Stage3InputError("Foreground exception ID lists are invalid")
    if any(len(value) != len(set(value)) for value in (expected_ids, included_ids, pending_ids)):
        raise Stage3InputError("Foreground exception ID lists contain duplicates")
    if pending_ids:
        raise Stage3InputError(f"Foreground exceptions are still pending: {pending_ids}")
    if set(expected_ids) != set(included_ids):
        raise Stage3InputError("Included foreground IDs do not match the accepted missing-object review")

    styles = read_json(style_path)
    if styles.get("schema_version") != 2:
        raise Stage3InputError("Style plan schema_version 2 is required for explicit geometry review")
    if styles.get("source_sha256") != source_hash or styles.get("text_plan_sha256") != plan_hash:
        raise Stage3InputError("Style plan source/text-plan binding is stale")
    style_records = styles.get("styles")
    if not isinstance(style_records, list):
        raise Stage3InputError("Style plan must contain a styles array")
    editable_ids = {item["id"] for item in items if item["classification"] == "editable"}
    style_by_id = _style_index(style_records, editable_ids)
    partial_selection = plan.get("partial_selection")
    if workflow_state and workflow_state.get("mode") == "partial":
        if not isinstance(partial_selection, dict) or partial_selection.get("status") != "ready":
            raise Stage3InputError("Partial mode has no ready selected/preserved partition")
        selected_ids = set(partial_selection.get("selected_ids", []))
        preserved_ids = set(partial_selection.get("preserved_inventory_ids", partial_selection.get("preserved_ocr_ids", [])))
        role_preserved_ids = {item["id"] for item in items if item.get("selection_role") == "preserve_in_background"}
        if (not editable_ids.issubset(selected_ids) or role_preserved_ids != preserved_ids
                or (workflow_state and workflow_state.get("workflow_version") == "2.1.0" and editable_ids != selected_ids)):
            raise Stage3InputError("Only explicitly selected items may become native Type layers")
        if selected_ids.intersection(preserved_ids):
            raise Stage3InputError("Selected and preserved partial-mode IDs overlap")
        if workflow_state and workflow_state.get("workflow_version") == "2.1.1":
            inventory_ids = set(role_inventory.get("item_ids", []))
            main_copy_ids = {item["id"] for item in items if item.get("text_role") == "main_copy"}
            if (not inventory_ids.issubset(set(by_id)) or not selected_ids.issubset(main_copy_ids)
                    or not editable_ids.issubset(selected_ids)
                    or set(background.get("background_review", {}).get("retained_item_ids", [])) != preserved_ids
                    or set(background.get("background_review", {}).get("cleared_item_ids", [])) != selected_ids):
                raise Stage3InputError("Reviewed text roles, selected main copy, and background retention review do not match exactly")
    text_inventory = []
    style_audit = []
    rotation_groups: dict[str, list[dict[str, Any]]] = {}
    for text_id in (item["id"] for item in items if item["classification"] == "editable"):
        item, style = by_id[text_id], style_by_id[text_id]
        original = item.get("text")
        lines = style.get("display_lines")
        if not isinstance(original, str) or not isinstance(lines, list) or not lines or any(not isinstance(s, str) for s in lines):
            raise Stage3InputError(f"{text_id}: missing source text or display_lines")
        if "".join(lines).replace("\r", "").replace("\n", "") != original.replace("\r", "").replace("\n", ""):
            raise Stage3InputError(f"{text_id}: display line breaks changed source wording")
        source_box = box(item.get("source_bbox_px"), canvas, f"text {text_id} source box")
        layout_box = box(style.get("layout_box_px"), canvas, f"text {text_id} layout box")
        layout_review = style.get("layout_review")
        if (not isinstance(layout_review, dict)
                or layout_review.get("status") not in {"reviewed_by_author", "pending_director_review"}
                or not isinstance(layout_review.get("note"), str) or not layout_review["note"].strip()):
            raise Stage3InputError(f"{text_id}: explicit layout_review status and note are required")
        weight = style.get("font_weight")
        size = style.get("font_size_pt")
        color = style.get("color_rgba")
        if weight not in WEIGHTS or type(size) not in (int, float) or size <= 0:
            raise Stage3InputError(f"{text_id}: invalid Source Han weight or size")
        if not isinstance(color, list) or len(color) not in (3, 4) or any(type(c) is not int or c < 0 or c > 255 for c in color):
            raise Stage3InputError(f"{text_id}: invalid color_rgba")
        color = color + [255] if len(color) == 3 else color
        alignment = style.get("alignment", "left")
        if alignment not in {"left", "center", "right"}:
            raise Stage3InputError(f"{text_id}: invalid alignment")
        vertical_alignment = _validated_vertical_alignment(style, text_id)
        rotation = style.get("rotation_deg")
        if type(rotation) not in (int, float) or not math.isfinite(float(rotation)):
            raise Stage3InputError(f"{text_id}: rotation_deg must be explicit and finite")
        rotation = float(rotation)
        geometry_review = style.get("geometry_review")
        if (not isinstance(geometry_review, dict) or geometry_review.get("status") != "reviewed"
                or not isinstance(geometry_review.get("reviewer"), str)
                or not geometry_review["reviewer"].strip()
                or not isinstance(geometry_review.get("note"), str) or not geometry_review["note"].strip()
                or not isinstance(geometry_review.get("basis"), str) or not geometry_review["basis"].strip()):
            raise Stage3InputError(f"{text_id}: geometry review must be completed with reviewer, basis, and evidence note")
        reviewed_rotation = geometry_review.get("reviewed_rotation_deg")
        if (type(reviewed_rotation) not in (int, float) or not math.isfinite(float(reviewed_rotation))
                or abs(float(reviewed_rotation) - rotation) > 1e-6):
            raise Stage3InputError(f"{text_id}: reviewed angle does not match rotation_deg")
        rotation_group_id = style.get("rotation_group_id")
        rotation_anchor = style.get("rotation_anchor_px")
        if rotation_group_id is not None:
            if not isinstance(rotation_group_id, str) or not SAFE_ID.fullmatch(rotation_group_id):
                raise Stage3InputError(f"{text_id}: rotation_group_id is invalid")
            if rotation_anchor is None:
                raise Stage3InputError(f"{text_id}: grouped rotation needs a shared rotation_anchor_px")
            rotation_anchor = point(rotation_anchor, canvas, f"{text_id} rotation_anchor_px")
            rotation_groups.setdefault(rotation_group_id, []).append({
                "id": text_id, "rotation_deg": rotation, "rotation_anchor_px": rotation_anchor})
        elif rotation_anchor is not None:
            raise Stage3InputError(f"{text_id}: rotation_anchor_px is only valid with rotation_group_id")
        leading = style.get("leading_pt")
        auto_leading = bool(style.get("auto_leading", True))
        if not auto_leading and (type(leading) not in (int, float) or leading <= 0):
            raise Stage3InputError(f"{text_id}: explicit leading must be positive")
        display_text = "\n".join(lines)
        style_runs = _validated_style_runs(style, display_text, text_id, int(style.get("tracking", 0)))
        text_inventory.append({
            "id": text_id, "text": display_text, "source_text": original,
            "source_box_px": source_box, "layout_box_px": layout_box,
            "font_weight": weight, "font_size_pt": float(size), "color_rgba": color,
            "tracking": int(style.get("tracking", 0)), "alignment": alignment,
            "vertical_alignment": vertical_alignment,
            "auto_leading": auto_leading, "leading_pt": float(leading or 0),
            "rotation_deg": rotation,
            **({"style_runs": style_runs} if style_runs is not None else {}),
            **({"rotation_group_id": rotation_group_id, "rotation_anchor_px": rotation_anchor}
               if rotation_group_id else {}),
        })
        style_audit.append({"text_item_id": text_id, **{k: v for k, v in style.items() if k != "text_item_id"},
                            "source_text": original, "display_text": display_text, "source_box_px": source_box})

    for group_id, members in rotation_groups.items():
        if len(members) < 2:
            raise Stage3InputError(f"Rotation group {group_id} needs at least two text items")
        angles = {member["rotation_deg"] for member in members}
        anchors = {tuple(member["rotation_anchor_px"]) for member in members}
        if len(angles) != 1 or len(anchors) != 1:
            raise Stage3InputError(f"Rotation group {group_id} members must share one reviewed angle and anchor")

    title_reviews: dict[str, tuple[Path, dict[str, Any], Path, str]] = {}
    for title_path in title_paths:
        review = read_json(title_path)
        title_id = review.get("text_item_id")
        title_item = by_id.get(title_id)
        if title_id in title_reviews:
            raise Stage3InputError(f"Duplicate title review for {title_id}")
        if (review.get("status") != "accepted" or not isinstance(review.get("reviewer"), str)
                or not review["reviewer"].strip() or not isinstance(title_item, dict)
                or title_item.get("classification") != "title_raster"):
            raise Stage3InputError("Each title review must be accepted and match a title_raster item")
        if (review.get("source_sha256") != source_hash or review.get("text_plan_sha256") != plan_hash
                or review.get("clean_base_sha256") != base_hash or review.get("text") != title_item.get("text")
                or review.get("source_bbox_px") != title_item.get("source_bbox_px")):
            raise Stage3InputError(f"Title review for {title_id} does not match plan/source/clean-base")
        title_asset = resolve_reference(title_path, review.get("asset_path"), f"accepted title {title_id}")
        title_hash = sha256_file(title_asset)
        if title_hash != review.get("asset_sha256"):
            raise Stage3InputError(f"Accepted title asset hash mismatch for {title_id}")
        title_reviews[title_id] = (title_path, review, title_asset, title_hash)
    if set(title_reviews) != expected_title_ids:
        raise Stage3InputError("Title review IDs must exactly cover the plan's title_raster items")

    foreground_path = Path(background.get("foreground_exceptions", {}).get("asset_manifest_path", "")).expanduser().resolve()
    if not foreground_path.is_file() or sha256_file(foreground_path) != background.get("foreground_exceptions", {}).get("asset_manifest_sha256"):
        raise Stage3InputError("Reviewed foreground exception manifest is missing or stale")
    foreground = read_json(foreground_path)
    records = foreground.get("foreground_assets")
    if not isinstance(records, list):
        raise Stage3InputError("Foreground exception manifest has no foreground_assets")
    manifest_ids = [record.get("visual_unit_id") for record in records if isinstance(record, dict)]
    if len(manifest_ids) != len(records) or len(manifest_ids) != len(set(manifest_ids)) or set(manifest_ids) != set(included_ids):
        raise Stage3InputError("Foreground manifest IDs do not match the background build inclusion record")

    run_root = Path(args.out_dir).expanduser().resolve()
    if run_root.exists() and any(run_root.iterdir()):
        raise Stage3InputError(f"Stage 3 run directory is not empty: {run_root}")
    run_root.mkdir(parents=True, exist_ok=True)
    source_copy = run_root / "source.png"
    shutil.copyfile(source_path, source_copy)
    if sha256_file(source_copy) != source_hash:
        raise Stage3InputError("Run-local source copy hash changed")
    root = run_root / "stage2"
    root.mkdir()
    copied_base = root / "clean-base.png"
    shutil.copyfile(base_path, copied_base)
    if sha256_file(copied_base) != base_hash:
        raise Stage3InputError("Copied clean-base hash changed")

    asset_records = []
    for record in records:
        asset_records.append(_asset_record(record, foreground_path, root, canvas, source_hash, plan_hash,
                                           base_hash, "accepted_foreground_exception"))
    for title_id, (title_path, title_review, _, title_hash) in title_reviews.items():
        asset_records.append(_asset_record({"id": title_id, "source_box_px": title_review["source_bbox_px"],
                                            "alpha_asset_path": title_review["asset_path"],
                                            "alpha_asset_sha256": title_hash}, title_path, root, canvas,
                                           source_hash, plan_hash, base_hash, "accepted_title_raster"))

    page = {
        "schema_version": 1, "status": "ready_for_stage_3",
        "workflow": plan.get("workflow"),
        "partial_selection": plan.get("partial_selection"),
        "source": {"source_png_path": "../source.png", "source_png_sha256": source_hash,
                   "size_px": {"width": width, "height": height}},
        "background": {"status": "ready", "decision": "imported_clean_base",
                       "path": copied_base.name, "sha256": base_hash},
        "foreground_assets": asset_records,
        "text_inventory": text_inventory,
        "text_plan_sha256": plan_hash, "style_plan_sha256": sha256_file(style_path),
        "background_build_sha256": sha256_file(background_path),
        "title_review_sha256s": {title_id: sha256_file(value[0]) for title_id, value in title_reviews.items()},
    }
    page_path = root / "page_rebuild.json"
    write_json(page_path, page)
    input_manifest = {
        "schema_version": 1, "status": "READY_FOR_TYSH_WRITER",
        "workflow": plan.get("workflow"),
        "partial_selection": plan.get("partial_selection"),
        "source_sha256": source_hash, "text_plan_sha256": plan_hash, "clean_base_sha256": base_hash,
        "background_build_sha256": sha256_file(background_path),
        "foreground_exception_manifest_sha256": sha256_file(foreground_path),
        "title_asset_sha256s": {title_id: value[3] for title_id, value in title_reviews.items()},
        "title_review_sha256s": {title_id: sha256_file(value[0]) for title_id, value in title_reviews.items()},
        "style_plan_sha256": sha256_file(style_path), "page_rebuild_sha256": sha256_file(root / "page_rebuild.json"),
        "text_layer_count": len(text_inventory), "raster_title_count": len(title_reviews),
        "foreground_exception_count": len(records),
        "pending_native_text_ids": [],
        "photoshop_manual_acceptance": "PENDING_MANUAL_PHOTOSHOP",
        "text_layers": style_audit,
        "run_local_source_path": str(source_copy),
    }
    input_manifest["page_rebuild_sha256"] = sha256_file(page_path)
    write_json(run_root / "assembly-input-manifest.json", input_manifest)
    return {"root": str(run_root), "page_rebuild_json": str(page_path),
            "manifest": input_manifest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--text-plan", required=True)
    parser.add_argument("--background-build", required=True)
    parser.add_argument("--style-plan", required=True)
    parser.add_argument("--title-reviews", nargs="*", default=[], help="zero or more accepted title review manifests")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare(args)
        print(json.dumps({"status": result["manifest"]["status"], "page_rebuild_json": result["page_rebuild_json"],
                          "text_layer_count": result["manifest"]["text_layer_count"]}, ensure_ascii=False))
        return 0
    except (Stage3InputError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
