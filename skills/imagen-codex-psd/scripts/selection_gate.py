#!/usr/bin/env python3
"""Persist explicit full/partial text-scope choices and source-bound selections."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

NEW_WORKFLOW_VERSION = "2.1.1"
SUPPORTED_WORKFLOW_VERSIONS = {"2.1.0", NEW_WORKFLOW_VERSION}


class SelectionError(ValueError):
    """The saved selection is missing, stale, invalid, or unresolved."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SelectionError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SelectionError(f"Expected a JSON object in {path}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def gate_path(run_dir: Path) -> Path:
    return run_dir / "selection-gate.json"


def require_current_role_review(gate: dict[str, Any]) -> None:
    if gate.get("workflow_version") != NEW_WORKFLOW_VERSION:
        return
    role_path = Path(gate.get("text_role_review_path", ""))
    if (not gate.get("text_role_review_sha256") or not role_path.is_file()
            or sha256_file(role_path) != gate.get("text_role_review_sha256")):
        raise SelectionError("Source text-role review changed after numbering; recompile it and show a fresh number map before selecting.")


def load_gate(run_dir: Path, source_hash: str | None = None) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    manifest = _read(manifest_path) if manifest_path.is_file() else {}
    workflow = manifest.get("workflow", {})
    version = workflow.get("version")
    if workflow.get("selection_required") is not True and version not in SUPPORTED_WORKFLOW_VERSIONS:
        # A 2.0.1 run is a legacy full-mode run. New runs set this manifest flag,
        # so deleting only the gate can never silently turn one into full mode.
        return {"schema_version": 0, "mode": "full", "status": "ready",
                "legacy_2_0_1_full_mode": True,
                "source_sha256": manifest.get("source", {}).get("coordinate_sha256")}
    path = gate_path(run_dir)
    if not path.is_file():
        raise SelectionError("This new run requires selection-gate.json; missing state cannot default to full mode.")
    gate = _read(path)
    expected = source_hash or manifest.get("source", {}).get("coordinate_sha256")
    if gate.get("source_sha256") != expected or gate.get("source_sha256") != manifest.get("source", {}).get("coordinate_sha256"):
        raise SelectionError("Selection state belongs to another source image.")
    if (gate.get("schema_version") != 1 or version not in SUPPORTED_WORKFLOW_VERSIONS
            or gate.get("workflow_version") != version):
        raise SelectionError("Selection state has an unsupported schema or workflow version.")
    return gate


def require_ready(run_dir: Path, source_hash: str | None = None) -> dict[str, Any]:
    gate = load_gate(run_dir, source_hash)
    if gate.get("status") != "ready" or gate.get("mode") not in {"full", "partial"}:
        raise SelectionError(f"Workflow selection is still pending ({gate.get('status', 'unknown')}); downstream work is blocked.")
    if gate.get("mode") == "partial":
        if not isinstance(gate.get("selected_ids"), list) or not gate["selected_ids"]:
            raise SelectionError("Partial mode needs at least one explicit selected OCR or screenshot target.")
        for target in gate.get("screenshot_targets", []):
            if target.get("status") != "confirmed":
                raise SelectionError(f"Screenshot target {target.get('id', '?')} has unresolved location or text.")
            shot = Path(target.get("evidence_path", ""))
            if not shot.is_file() or sha256_file(shot) != target.get("screenshot_sha256"):
                raise SelectionError(f"Screenshot evidence for {target.get('id', '?')} is missing or changed.")
        if gate.get("workflow_version") == NEW_WORKFLOW_VERSION:
            require_current_role_review(gate)
    return gate


def require_ocr_authorized(run_dir: Path, source_hash: str | None = None) -> dict[str, Any]:
    gate = load_gate(run_dir, source_hash)
    if gate.get("legacy_2_0_1_full_mode"):
        return gate
    allowed = ((gate.get("mode") == "full" and gate.get("status") == "ready")
               or (gate.get("mode") == "partial" and gate.get("status") == "partial_waiting_ocr"))
    if not allowed:
        raise SelectionError("OCR is blocked until the user explicitly answers A/full or B/partial for this source.")
    return gate


def initialize(run_dir: Path, source_hash: str) -> Path:
    path = gate_path(run_dir)
    if path.exists():
        existing = _read(path)
        if existing.get("source_sha256") != source_hash:
            raise SelectionError("Run already has selection state for another source image.")
        return path
    manifest = _read(run_dir / "manifest.json")
    workflow_version = manifest.get("workflow", {}).get("version", "2.1.0")
    if workflow_version not in SUPPORTED_WORKFLOW_VERSIONS:
        raise SelectionError(f"Cannot initialize unsupported selection workflow {workflow_version}.")
    _write(path, {
        "schema_version": 1,
        "workflow_version": workflow_version,
        "source_sha256": source_hash,
        "status": "pending_mode",
        "mode": None,
        "selected_ids": None,
        "selected_numbers": None,
        "ocr_sha256": None,
        "number_map_sha256": None,
        "screenshot_targets": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notes": "No mode or selection is inferred; downstream stages remain blocked until an explicit answer is recorded.",
    })
    return path


def record_mode(run_dir: Path, mode: str) -> dict[str, Any]:
    if mode not in {"full", "partial"}:
        raise SelectionError("Mode answer must be full or partial.")
    gate = load_gate(run_dir)
    if gate.get("legacy_2_0_1_full_mode"):
        raise SelectionError("Mode answers apply only to a new selection-required run.")
    if gate.get("status") != "pending_mode":
        raise SelectionError("Mode choice is not pending; refusing to overwrite an existing workflow choice.")
    gate.update({"mode": mode, "status": "ready" if mode == "full" else "partial_waiting_ocr",
                 "mode_answer_recorded_at": datetime.now(timezone.utc).isoformat()})
    _write(gate_path(run_dir), gate)
    return gate


def _ordered_candidates(ocr: dict[str, Any], source_hash: str) -> list[dict[str, Any]]:
    if ocr.get("source", {}).get("sha256") != source_hash:
        raise SelectionError("OCR candidate artifact belongs to another coordinate image.")
    candidates = ocr.get("candidates")
    if not isinstance(candidates, list):
        raise SelectionError("OCR artifact must contain candidates.")
    items = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str):
            raise SelectionError("Every OCR candidate needs a stable OCR ID.")
        if candidate["id"] in seen:
            raise SelectionError(f"Duplicate OCR candidate ID: {candidate['id']}")
        seen.add(candidate["id"])
        box = candidate.get("box_px")
        text = candidate.get("text_candidate")
        if (not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box)
                or not isinstance(text, str) or not text.strip()):
            raise SelectionError(f"{candidate['id']}: text or source box is invalid.")
        x, y, w, h = box
        if min(x, y) < 0 or min(w, h) <= 0:
            raise SelectionError(f"{candidate['id']}: source box is invalid.")
        items.append({"ocr_id": candidate["id"], "text": text, "source_bbox_px": box,
                      "sort_key": [y, x, candidate["id"]]})
    return sorted(items, key=lambda row: tuple(row["sort_key"]))


def build_number_map(run_dir: Path, ocr_path: Path, role_review_path: Path | None = None) -> dict[str, Any]:
    manifest = _read(run_dir / "manifest.json")
    source_hash = manifest.get("source", {}).get("coordinate_sha256")
    gate = require_mode_for_map(run_dir, source_hash)
    if gate.get("mode") != "partial":
        raise SelectionError("OCR numbering is only used after the user chooses partial mode.")
    ocr_path = ocr_path.expanduser().resolve()
    if not ocr_path.is_file():
        raise SelectionError("OCR candidate artifact is missing.")
    ocr_hash = sha256_file(ocr_path)
    ocr = _read(ocr_path)
    if gate.get("workflow_version") == NEW_WORKFLOW_VERSION:
        if role_review_path is None:
            role_review_path = Path(gate.get("text_role_review_path", ""))
        if not role_review_path:
            raise SelectionError("2.1.1 numbering requires the completed source-bound text-role review.")
        role_review_path = role_review_path.expanduser().resolve()
        if not role_review_path.is_file():
            raise SelectionError("Completed source text-role review file is missing.")
        try:
            from text_roles import TextRoleError, validate_review
            manifest_source = manifest.get("source", {})
            reviewed_items = validate_review(
                _read(role_review_path), source_hash=source_hash, raw_ocr_hash=ocr_hash,
                raw_ocr=ocr, width=manifest_source.get("width_px"), height=manifest_source.get("height_px"))
        except (TextRoleError, TypeError) as exc:
            raise SelectionError(str(exc)) from exc
        candidates = []
        for item in reviewed_items:
            if item.get("text_role") != "main_copy":
                continue
            candidates.append({"ocr_id": item["id"], "text": item["text"],
                               "source_bbox_px": item["source_bbox_px"],
                               "role_reason": item["review_reason"],
                               "source_ocr_ids": item["source_ocr_ids"]})
        if not candidates:
            raise SelectionError("Reviewed inventory has no selectable main-copy items.")
        role_review_sha256 = sha256_file(role_review_path)
    else:
        candidates = _ordered_candidates(ocr, source_hash)
        role_review_sha256 = None
    source_path = Path(manifest.get("source", {}).get("coordinate_path", ""))
    if not source_path.is_absolute():
        source_path = run_dir / source_path
    if not source_path.is_file() or sha256_file(source_path) != source_hash:
        raise SelectionError("Prepared source image is missing or has changed.")
    with Image.open(source_path) as original:
        original.load()
        source_size = list(original.size)
        image = original.convert("RGB").resize((original.width * 2, original.height * 2), Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(image)
    font_path = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
    mapping = []
    for number, candidate in enumerate(candidates, start=1):
        x, y, w, h = candidate["source_bbox_px"]
        if x + w > source_size[0] or y + h > source_size[1]:
            raise SelectionError(f"{candidate['ocr_id']}: OCR source box is outside the source image.")
        sx, sy, sw, sh = x * 2, y * 2, w * 2, h * 2
        draw.rectangle((sx, sy, sx + sw - 1, sy + sh - 1), outline=(255, 220, 0), width=4)
        label = str(number)
        font_size = max(24, min(54, int(max(sh, 24) * 0.9), int(image.width * 0.12)))
        try:
            font = ImageFont.truetype(str(font_path), font_size) if font_path.is_file() else ImageFont.load_default()
        except OSError:
            font = ImageFont.load_default()
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font, stroke_width=0)
        tag_w, tag_h = right - left + 14, bottom - top + 12
        # Keep labels readable at normal zoom on dark, light, and mixed artwork.
        tag_x = max(0, min(sx, image.width - tag_w))
        tag_y = sy - tag_h - 6 if sy >= tag_h + 6 else sy + 2
        tag_y = max(0, min(tag_y, image.height - tag_h))
        draw.rounded_rectangle((tag_x, tag_y, tag_x + tag_w, tag_y + tag_h), radius=5,
                               fill=(255, 230, 65), outline=(0, 0, 0), width=4)
        draw.text((tag_x + 7, tag_y + 4), label, fill=(0, 0, 0), font=font,
                  stroke_width=5, stroke_fill=(0, 0, 0))
        draw.text((tag_x + 7, tag_y + 4), label, fill=(0, 0, 0), font=font,
                  stroke_width=2, stroke_fill=(255, 255, 255))
        draw.text((tag_x + 7, tag_y + 4), label, fill=(0, 0, 0), font=font)
        if (tag_x, tag_y + tag_h // 2) != (sx, sy):
            draw.line((tag_x + tag_w // 2, tag_y + tag_h, sx + min(sw // 2, 12), sy), fill=(0, 0, 0), width=3)
            draw.line((tag_x + tag_w // 2, tag_y + tag_h + 1, sx + min(sw // 2, 12), sy + 1), fill=(255, 255, 255), width=1)
        record = {"number": number, "ocr_id": candidate["ocr_id"], "inventory_id": candidate["ocr_id"],
                  "text": candidate["text"], "source_bbox_px": candidate["source_bbox_px"]}
        if "source_ocr_ids" in candidate:
            record.update({"source_ocr_ids": candidate["source_ocr_ids"], "role_reason": candidate["role_reason"]})
        mapping.append(record)
    version_key = f"{ocr_hash[:12]}-roles-{role_review_sha256[:12]}" if role_review_sha256 else ocr_hash[:12]
    overlay_path = run_dir / f"ocr-numbered-overlay-{version_key}.png"
    image.save(overlay_path, format="PNG")
    mapping_doc = {
        "schema_version": 1,
        "source_sha256": source_hash,
        "ocr_sha256": ocr_hash,
        "raw_ocr_sha256": ocr_hash,
        "role_review_sha256": role_review_sha256,
        "numbered_role": "main_copy only" if role_review_sha256 else "all OCR candidates",
        "ordering": "top-to-bottom, then left-to-right, then stable inventory ID; 1-based contiguous",
        "display_scale": 2,
        "source_size_px": source_size,
        "preview_size_px": list(image.size),
        "preview_coordinates": "Numbers/boxes are shown at 2x for readability; all mapping source_bbox_px values remain on the source.png pixel grid.",
        "preview_path": str(overlay_path),
        "preview_sha256": sha256_file(overlay_path),
        "items": mapping,
    }
    mapping_path = run_dir / f"ocr-number-map-{version_key}.json"
    _write(mapping_path, mapping_doc)
    map_hash = sha256_file(mapping_path)
    gate.update({"ocr_sha256": ocr_hash, "number_map_path": str(mapping_path),
                 "ocr_artifact_path": str(ocr_path),
                 "number_map_sha256": map_hash, "numbered_overlay_path": str(overlay_path),
                 "numbered_overlay_sha256": sha256_file(overlay_path), "status": "pending_partial_method"})
    if role_review_sha256:
        gate.update({"text_role_review_path": str(role_review_path),
                     "text_role_review_sha256": role_review_sha256,
                     "raw_ocr_path": str(ocr_path)})
    _write(gate_path(run_dir), gate)
    return mapping_doc


def require_mode_for_map(run_dir: Path, source_hash: str | None) -> dict[str, Any]:
    gate = load_gate(run_dir, source_hash)
    if gate.get("status") not in {"partial_waiting_ocr", "pending_partial_method", "pending_numbers"}:
        raise SelectionError(f"Number mapping is not expected in state {gate.get('status')}.")
    return gate


def parse_numbers(answer: str, count: int) -> list[int]:
    if not isinstance(answer, str) or not answer.strip():
        raise SelectionError("Select at least one OCR number; empty input never means all text.")
    pieces = re.split(r"[,，、;；\s]+", answer.strip())
    if not pieces or any(not re.fullmatch(r"[0-9]+", token) for token in pieces if token):
        raise SelectionError("Selection contains an invalid number; use comma, Chinese comma, enumeration comma, semicolon, or spaces.")
    if not all(pieces):
        raise SelectionError("Selection contains an empty number.")
    numbers = [int(piece) for piece in pieces]
    if len(numbers) != len(set(numbers)):
        raise SelectionError("Duplicate selection numbers are not allowed.")
    outside = [value for value in numbers if value < 1 or value > count]
    if outside:
        raise SelectionError(f"Selection number is out of range: {outside}; valid range is 1..{count}.")
    return numbers


def record_method(run_dir: Path, choice: str) -> dict[str, Any]:
    if choice not in {"numbers", "screenshot"}:
        raise SelectionError("Partial method must be numbers or screenshot.")
    gate = load_gate(run_dir)
    require_current_role_review(gate)
    if gate.get("mode") != "partial" or gate.get("status") != "pending_partial_method":
        raise SelectionError("The partial-method choice is not pending.")
    gate.update({"partial_method": "ocr_numbers" if choice == "numbers" else "unreadable_screenshot",
                 "status": "pending_numbers" if choice == "numbers" else "awaiting_screenshot",
                 "partial_method_recorded_at": datetime.now(timezone.utc).isoformat()})
    _write(gate_path(run_dir), gate)
    return gate


def record_numbers(run_dir: Path, answer: str) -> dict[str, Any]:
    gate = load_gate(run_dir)
    require_current_role_review(gate)
    if gate.get("mode") != "partial" or gate.get("status") not in {"pending_partial_method", "pending_numbers"}:
        raise SelectionError("OCR selection is not pending.")
    mapping_path = Path(gate.get("number_map_path", ""))
    if (not mapping_path.is_file() or sha256_file(mapping_path) != gate.get("number_map_sha256")):
        raise SelectionError("Displayed OCR number map is stale or missing; build and show a fresh map before accepting numbers.")
    mapping = _read(mapping_path)
    current_ocr_path = Path(gate.get("ocr_artifact_path", ""))
    if not current_ocr_path.is_file() or sha256_file(current_ocr_path) != gate.get("ocr_sha256"):
        raise SelectionError("OCR artifact changed after the numbered overlay was shown; rebuild and show a fresh map.")
    if mapping.get("source_sha256") != gate.get("source_sha256") or mapping.get("ocr_sha256") != gate.get("ocr_sha256"):
        raise SelectionError("Displayed OCR number map belongs to another source or OCR artifact.")
    numbers = parse_numbers(answer, len(mapping.get("items", [])))
    by_number = {item["number"]: item for item in mapping["items"]}
    selected = [by_number[n] for n in numbers]
    gate.update({"selected_numbers": numbers, "selected_ids": [item["ocr_id"] for item in selected],
                 "selected_items": selected, "partial_method": "ocr_numbers", "status": "ready",
                 "selection_answer_recorded_at": datetime.now(timezone.utc).isoformat()})
    _write(gate_path(run_dir), gate)
    return gate


def _parse_box(answer: str, width: int, height: int) -> list[int]:
    parts = re.split(r"[,，\s]+", answer.strip()) if isinstance(answer, str) else []
    if len(parts) != 4 or any(not re.fullmatch(r"\d+", part) for part in parts):
        raise SelectionError("Source box must be x,y,width,height in source.png pixels.")
    x, y, w, h = map(int, parts)
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
        raise SelectionError("Source box is outside source.png.")
    return [x, y, w, h]


def _overlaps(a: list[int], b: list[int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _matching_reviewed_main_copy(run_dir: Path, gate: dict[str, Any], target: dict[str, Any]) -> str | None:
    if gate.get("workflow_version") != NEW_WORKFLOW_VERSION or not target.get("source_bbox_px"):
        return None
    role_path = Path(gate.get("text_role_review_path", ""))
    if not role_path.is_file() or sha256_file(role_path) != gate.get("text_role_review_sha256"):
        return None
    target_box = target["source_bbox_px"]
    tx, ty, tw, th = target_box
    matches = []
    for item in _read(role_path).get("items", []):
        if not isinstance(item, dict) or item.get("text_role") != "main_copy" or item.get("text") != target.get("text"):
            continue
        box = item.get("source_bbox_px")
        if not isinstance(box, list) or len(box) != 4 or not _overlaps(target_box, box):
            continue
        x, y, w, h = box
        intersection = max(0, min(tx + tw, x + w) - max(tx, x)) * max(0, min(ty + th, y + h) - max(ty, y))
        smaller_area = min(tw * th, w * h)
        if smaller_area > 0 and intersection / smaller_area >= 0.75:
            matches.append(item.get("id"))
    return matches[0] if len(matches) == 1 and isinstance(matches[0], str) else None


def add_screenshot_target(run_dir: Path, screenshot_arg: Path, source_box: str | None,
                          text: str | None, ambiguous: bool = False) -> dict[str, Any]:
    gate = load_gate(run_dir)
    require_current_role_review(gate)
    if gate.get("mode") != "partial" or gate.get("status") != "awaiting_screenshot":
        raise SelectionError("Screenshot attachment is not currently requested by the user-selected branch.")
    manifest = _read(run_dir / "manifest.json")
    source = manifest["source"]
    source_path = Path(source["coordinate_path"])
    if not source_path.is_absolute():
        source_path = run_dir / source_path
    if not source_path.is_file() or sha256_file(source_path) != gate.get("source_sha256"):
        raise SelectionError("Prepared source image is missing or changed.")
    screenshot = screenshot_arg.expanduser().resolve()
    if not screenshot.is_file():
        raise SelectionError("The requested screenshot attachment is missing.")
    if ambiguous and (source_box or text):
        raise SelectionError("An ambiguous location cannot carry guessed coordinates or text; ask the user to identify it first.")
    if not ambiguous and (not source_box or not isinstance(text, str) or not text.strip()):
        raise SelectionError("A confirmed screenshot target needs an exact source box and verified text.")
    box = _parse_box(source_box, source["width_px"], source["height_px"]) if source_box else None
    evidence_dir = run_dir / "input" / "selection-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    index = len(gate.get("screenshot_targets", [])) + 1
    target_id = f"M{index:03d}"
    saved_screenshot = evidence_dir / f"{target_id}{screenshot.suffix.lower()}"
    shutil.copy2(screenshot, saved_screenshot)
    screenshot_hash = sha256_file(saved_screenshot)
    target = {"id": target_id, "screenshot_original_path": str(screenshot),
              "evidence_path": str(saved_screenshot), "screenshot_sha256": screenshot_hash,
              "source_sha256": gate["source_sha256"], "source_bbox_px": box,
              "text": text.strip() if isinstance(text, str) else None,
              "status": "pending_location_confirmation" if ambiguous else "confirmed",
              "confirmed_by": None if ambiguous else "visual_source_alignment",
              "recorded_at": datetime.now(timezone.utc).isoformat()}
    gate.setdefault("screenshot_targets", []).append(target)
    gate["partial_method"] = "unreadable_screenshot"
    gate["status"] = "pending_location_confirmation" if ambiguous else "ready"
    gate["selected_ids"] = [] if ambiguous else [target_id]
    _write(gate_path(run_dir), gate)
    if not ambiguous:
        matched_item_id = _matching_reviewed_main_copy(run_dir, gate, target)
        if matched_item_id:
            target["duplicate_of_inventory_id"] = matched_item_id
            gate["selected_ids"] = [matched_item_id]
            target["overlap_resolution"] = "screenshot_matches_reviewed_main_copy"
            target["overlap_candidate_ids"] = [matched_item_id]
            _write(gate_path(run_dir), gate)
        overlaps = find_target_overlaps(run_dir, target)
        if overlaps and not (matched_item_id and set(overlaps) == {matched_item_id}):
            gate["status"] = "pending_overlap_confirmation"
            target["overlap_candidate_ids"] = overlaps
            target["status"] = "pending_overlap_confirmation"
            _write(gate_path(run_dir), gate)
    return gate


def find_target_overlaps(run_dir: Path, target: dict[str, Any]) -> list[str]:
    if not target.get("source_bbox_px"):
        return []
    ocr_path = run_dir / "ocr-first" / "ocr-candidates.json"
    gate = load_gate(run_dir)
    if gate.get("workflow_version") == NEW_WORKFLOW_VERSION:
        role_path = Path(gate.get("text_role_review_path", ""))
        if role_path.is_file() and sha256_file(role_path) == gate.get("text_role_review_sha256"):
            role_doc = _read(role_path)
            candidate_boxes = [(item.get("id"), item.get("source_bbox_px"))
                               for item in role_doc.get("items", []) if isinstance(item, dict)]
            return [str(candidate_id) for candidate_id, box in candidate_boxes
                    if isinstance(candidate_id, str) and isinstance(box, list)
                    and _overlaps(target["source_bbox_px"], box)]
    mapping_path = Path(gate.get("number_map_path", ""))
    if mapping_path.is_file():
        mapping = _read(mapping_path)
        candidate_boxes = [(item["ocr_id"], item["source_bbox_px"]) for item in mapping.get("items", [])]
    elif ocr_path.is_file():
        candidate_boxes = [(item.get("id"), item.get("box_px")) for item in _read(ocr_path).get("candidates", [])]
    else:
        candidate_boxes = []
    return [str(candidate_id) for candidate_id, box in candidate_boxes
            if isinstance(candidate_id, str) and isinstance(box, list) and _overlaps(target["source_bbox_px"], box)]


def confirm_target(run_dir: Path, target_id: str, source_box: str, text: str,
                   confirmed_by: str, *, confirm_overlap_distinct: bool = False) -> dict[str, Any]:
    gate = load_gate(run_dir)
    require_current_role_review(gate)
    if gate.get("status") not in {"pending_location_confirmation", "pending_overlap_confirmation"}:
        raise SelectionError("No screenshot target is awaiting location confirmation.")
    manifest = _read(run_dir / "manifest.json")
    source = manifest["source"]
    target = next((item for item in gate.get("screenshot_targets", []) if item.get("id") == target_id), None)
    if not target:
        raise SelectionError(f"Unknown screenshot target {target_id}.")
    target["source_bbox_px"] = _parse_box(source_box, source["width_px"], source["height_px"])
    if not isinstance(text, str) or not text.strip() or not isinstance(confirmed_by, str) or not confirmed_by.strip():
        raise SelectionError("Location confirmation needs verified text and a user/reviewer identity.")
    target["text"] = text.strip()
    target["confirmed_by"] = confirmed_by.strip()
    overlaps = find_target_overlaps(run_dir, target)
    if overlaps:
        matched_item_id = _matching_reviewed_main_copy(run_dir, gate, target)
        if matched_item_id and set(overlaps) == {matched_item_id}:
            target["duplicate_of_inventory_id"] = matched_item_id
            target["overlap_resolution"] = "screenshot_matches_reviewed_main_copy"
            target["overlap_candidate_ids"] = [matched_item_id]
            gate["selected_ids"] = [matched_item_id]
            gate["status"] = "ready"
            target["status"] = "confirmed"
            target["confirmed_by"] = confirmed_by.strip()
            _write(gate_path(run_dir), gate)
            return gate
        selected_ocr_ids = set(gate.get("selected_ids") or [])
        selected_conflicts = selected_ocr_ids.intersection(overlaps)
        if selected_conflicts:
            raise SelectionError("Screenshot target overlaps already-selected OCR IDs " + ", ".join(sorted(selected_conflicts)) + "; use that numbered OCR block instead of adding the same region again.")
        if not confirm_overlap_distinct:
            raise SelectionError("Screenshot target overlaps OCR IDs " + ", ".join(overlaps) + "; compare the exact source text and explicitly confirm whether this is a distinct missing target before continuing.")
        target["overlap_resolution"] = "user_confirmed_target_is_distinct_from_preserved_ocr_text"
        target["overlap_candidate_ids"] = overlaps
    target["status"] = "confirmed"
    target["confirmed_at"] = datetime.now(timezone.utc).isoformat()
    gate["status"] = "ready"
    gate["selected_ids"] = [target_id]
    _write(gate_path(run_dir), gate)
    return gate


def _command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    mode = sub.add_parser("record-mode", help="persist the actual user answer: full or partial")
    mode.add_argument("--run-dir", required=True, type=Path)
    mode.add_argument("--answer", choices=["full", "partial"], required=True)
    number_map = sub.add_parser("number-map", help="write a stable numbered OCR overlay and text/ID map")
    number_map.add_argument("--run-dir", required=True, type=Path)
    number_map.add_argument("--ocr-candidates", required=True, type=Path)
    number_map.add_argument("--role-review", type=Path, help="required for a new 2.1.1 run")
    numbers = sub.add_parser("record-numbers", help="persist the actual user's selected visible numbers")
    numbers.add_argument("--run-dir", required=True, type=Path)
    numbers.add_argument("--answer", required=True)
    method = sub.add_parser("record-method", help="persist the partial-mode choice: OCR numbers or unreadable screenshot")
    method.add_argument("--run-dir", required=True, type=Path)
    method.add_argument("--answer", choices=["numbers", "screenshot"], required=True)
    shot = sub.add_parser("record-screenshot", help="bind a user screenshot to a precisely mapped source box")
    shot.add_argument("--run-dir", required=True, type=Path)
    shot.add_argument("--screenshot", required=True, type=Path)
    shot.add_argument("--source-box", help="x,y,width,height on source.png; omit only with --ambiguous")
    shot.add_argument("--text", help="verified transcription; omit only with --ambiguous")
    shot.add_argument("--ambiguous", action="store_true", help="save evidence while keeping the workflow blocked")
    confirm = sub.add_parser("confirm-screenshot", help="record a user's clarified source location and exact text")
    confirm.add_argument("--run-dir", required=True, type=Path)
    confirm.add_argument("--target-id", required=True)
    confirm.add_argument("--source-box", required=True)
    confirm.add_argument("--text", required=True)
    confirm.add_argument("--confirmed-by", required=True)
    confirm.add_argument("--confirm-overlap-distinct", action="store_true",
                         help="explicitly record user confirmation that the target is distinct from overlapping OCR text")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _command_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "record-mode":
            result = record_mode(args.run_dir.expanduser().resolve(), args.answer)
        elif args.command == "number-map":
            result = build_number_map(args.run_dir.expanduser().resolve(), args.ocr_candidates, args.role_review)
            print(json.dumps({"status": "number_map_ready", "preview": result["preview_path"],
                              "items": [{"number": item["number"],
                                         "text": item["text"][:48] + ("…" if len(item["text"]) > 48 else "")}
                                        for item in result["items"]]}, ensure_ascii=False))
            return 0
        elif args.command == "record-numbers":
            result = record_numbers(args.run_dir.expanduser().resolve(), args.answer)
        elif args.command == "record-method":
            result = record_method(args.run_dir.expanduser().resolve(), args.answer)
        elif args.command == "record-screenshot":
            result = add_screenshot_target(args.run_dir.expanduser().resolve(), args.screenshot,
                                           args.source_box, args.text, args.ambiguous)
        elif args.command == "confirm-screenshot":
            result = confirm_target(args.run_dir.expanduser().resolve(), args.target_id,
                                    args.source_box, args.text, args.confirmed_by,
                                    confirm_overlap_distinct=args.confirm_overlap_distinct)
        else:
            raise SelectionError("Unknown command.")
        print(json.dumps({"status": result.get("status"), "mode": result.get("mode"),
                          "selected_numbers": result.get("selected_numbers"),
                          "selected_ids": result.get("selected_ids"),
                          "screenshot_targets": [{"id": t.get("id"), "status": t.get("status"),
                                                  "source_bbox_px": t.get("source_bbox_px"),
                                                  "overlap_candidate_ids": t.get("overlap_candidate_ids", [])}
                                                 for t in result.get("screenshot_targets", [])]}, ensure_ascii=False))
        return 0
    except (SelectionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
