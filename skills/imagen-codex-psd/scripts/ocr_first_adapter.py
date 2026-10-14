#!/usr/bin/env python3
"""Single-image PaddleOCR-VL adapter; response JSONL is saved before parsing."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path

import requests
from PIL import Image

from ocr_credentials import resolve_credential

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
DEFAULT_MODEL = "PaddleOCR-VL-1.6"
TEXT_LABELS = {"text", "paragraph_title", "vision_footnote", "doc_title"}


class OCRAdapterError(RuntimeError):
    pass


def _polygon_geometry(block: dict, scale_x: float, scale_y: float,
                      width: int, height: int) -> dict | None:
    raw_points = block.get("block_polygon_points")
    if not isinstance(raw_points, list) or not 3 <= len(raw_points) <= 128:
        return None
    points: list[list[float]] = []
    for point in raw_points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        try:
            x, y = float(point[0]) * scale_x, float(point[1]) * scale_y
        except (TypeError, ValueError):
            return None
        if not math.isfinite(x) or not math.isfinite(y) or x < 0 or y < 0 or x > width or y > height:
            return None
        points.append([round(x, 4), round(y, 4)])

    geometry = {"kind": "quadrilateral" if len(points) == 4 else "polygon",
                "points_px": points, "suggested_rotation_deg": None,
                "suggestion_status": "available_as_shape_reference"}
    if len(points) != 4:
        geometry["suggestion_basis"] = None
        return geometry

    # Only an actual four-corner convex polygon receives a baseline suggestion.
    # Multi-point text contours are preserved as references, never truncated to four points.
    crosses = []
    for index in range(4):
        ax, ay = points[index]
        bx, by = points[(index + 1) % 4]
        cx, cy = points[(index + 2) % 4]
        crosses.append((bx - ax) * (cy - by) - (by - ay) * (cx - bx))
    if any(abs(value) < 1e-5 for value in crosses) or not (all(v > 0 for v in crosses) or all(v < 0 for v in crosses)):
        geometry["suggestion_basis"] = None
        return geometry
    ordered = sorted(points, key=lambda p: (p[0], p[1]))
    left, right = ordered[:2], ordered[2:]
    left_x = sum(p[0] for p in left) / 2
    right_x = sum(p[0] for p in right) / 2
    dx = right_x - left_x
    if dx <= 0:
        geometry["suggestion_basis"] = None
        return geometry
    dy = sum(p[1] for p in right) / 2 - sum(p[1] for p in left) / 2
    angle = math.degrees(math.atan2(dy, dx))
    if abs(angle) > 45:
        geometry["suggestion_basis"] = None
        return geometry
    geometry["suggested_rotation_deg"] = round(angle, 3)
    geometry["suggestion_basis"] = "confirmed_convex_quad_left_to_right_midline"
    return geometry


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(data, encoding="utf-8")
    temp.replace(path)


def _save_state(path: Path, state: dict) -> None:
    _atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _redact_result(raw: str, token: str) -> str:
    if token:
        raw = raw.replace(token, "[REDACTED]")
    return re.sub(
        r'("[^"\n]*(?:url|token|authorization|signature|secret)[^"\n]*"\s*:\s*")[^"\n]*(")',
        r"\1[REDACTED]\2", raw, flags=re.IGNORECASE,
    )


def parse_result(source: Path, raw_jsonl: str) -> dict:
    pages = []
    for line in raw_jsonl.splitlines():
        if line.strip():
            for item in json.loads(line)["result"]["layoutParsingResults"]:
                pages.append(item["prunedResult"])
    if len(pages) != 1:
        raise OCRAdapterError(f"Expected one OCR page, got {len(pages)}")
    with Image.open(source) as image:
        width, height = image.size
    pruned = pages[0]
    scale_x = width / float(pruned.get("width") or width)
    scale_y = height / float(pruned.get("height") or height)
    candidates = []
    for block in pruned.get("parsing_res_list", []):
        if block.get("block_label") not in TEXT_LABELS:
            continue
        text = str(block.get("block_content", "")).strip()
        bbox = block.get("block_bbox")
        if not text or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        try:
            bx1, by1, bx2, by2 = (float(value) for value in bbox)
        except (TypeError, ValueError):
            continue
        x1, y1 = max(0, min(width, int(round(bx1 * scale_x)))), max(0, min(height, int(round(by1 * scale_y))))
        x2, y2 = max(0, min(width, int(round(bx2 * scale_x)))), max(0, min(height, int(round(by2 * scale_y))))
        if x2 <= x1 or y2 <= y1:
            continue
        candidate = {"text_candidate": text, "block_label": block.get("block_label"),
                     "box_px": [x1, y1, x2 - x1, y2 - y1], "box_source": "ocr-block"}
        geometry = _polygon_geometry(block, scale_x, scale_y, width, height)
        if geometry is not None:
            candidate["source_geometry"] = geometry
        candidates.append(candidate)
    candidates.sort(key=lambda item: (item["box_px"][1], item["box_px"][0]))
    candidates = [{"id": f"P{index:02d}", **candidate} for index, candidate in enumerate(candidates, 1)]
    return {
        "schema_version": 1,
        "backend": "paddleocr-vl",
        "advisory_only": True,
        "source": {"path": str(source), "width_px": width, "height_px": height,
                   "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
        "coordinate_space": "source.png pixels; box_px is [x, y, width, height]",
        "candidates": candidates,
        "note": "OCR may miss text or misrecognize characters; verify every candidate and inspect the full source image.",
    }


def persist_then_parse(source: Path, out_dir: Path, raw_jsonl: str, state: dict, token: str = "") -> dict:
    """Persist raw first, so parser or presentation failures cannot lose it."""
    raw_path = out_dir / "ocr-response.jsonl"
    clean_raw = _redact_result(raw_jsonl, token)
    _atomic_write(raw_path, clean_raw)
    state.update({"status": "raw_saved", "raw_response": raw_path.name})
    _save_state(out_dir / "ocr-job.json", state)
    candidates = parse_result(source, clean_raw)
    candidates.update({"job_id": state.get("job_id"), "advisory_only": True})
    _atomic_write(out_dir / "ocr-candidates.json", json.dumps(candidates, ensure_ascii=False, indent=2) + "\n")
    state["status"] = "complete"
    _save_state(out_dir / "ocr-job.json", state)
    return candidates


def _get_job_result(job_id: str, token: str) -> tuple[str, str]:
    headers = {"Authorization": f"bearer {token}"}
    try:
        response = requests.get(f"{JOB_URL}/{job_id}", headers=headers, timeout=(5, 20))
        response.raise_for_status()
        data = response.json()["data"]
    except Exception as exc:
        raise OCRAdapterError(f"Job status lookup failed ({type(exc).__name__}); rerun to resume this job.") from None
    state = str(data.get("state", "unknown"))
    if state != "done":
        return state, ""
    result_url = data.get("resultUrl", {}).get("jsonUrl")
    if not isinstance(result_url, str) or not result_url:
        raise OCRAdapterError("Completed job has no JSON result URL.")
    try:
        result = requests.get(result_url, timeout=(5, 60))
        result.raise_for_status()
    except Exception as exc:
        raise OCRAdapterError(f"Job result download failed ({type(exc).__name__}); rerun to resume this job.") from None
    return state, result.text


def run_one_page(source: Path, out_dir: Path) -> dict:
    source = source.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    if source.name != "source.png" or not source.is_file():
        raise OCRAdapterError("Input must be an existing single-page source.png")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path = source.parent / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OCRAdapterError(f"Cannot validate run selection state: {exc}") from exc
        workflow = manifest.get("workflow", {})
        if workflow.get("selection_required") is True or workflow.get("version") in {"2.1.0", "2.1.1"}:
            from selection_gate import SelectionError, require_ocr_authorized
            try:
                require_ocr_authorized(source.parent, digest)
            except SelectionError as exc:
                raise OCRAdapterError(str(exc)) from exc
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path, candidates_path, state_path = (out_dir / name for name in ("ocr-response.jsonl", "ocr-candidates.json", "ocr-job.json"))
    if raw_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        saved_digest = state.get("source_sha256")
        if not saved_digest and candidates_path.exists():
            saved_digest = json.loads(candidates_path.read_text(encoding="utf-8")).get("source", {}).get("sha256")
        if saved_digest and saved_digest != digest:
            raise OCRAdapterError("Saved OCR response belongs to a different source.png; refusing to reuse it.")
        if candidates_path.exists():
            return json.loads(candidates_path.read_text(encoding="utf-8"))
        return persist_then_parse(source, out_dir, raw_path.read_text(encoding="utf-8"), state)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if state and state.get("source_sha256") != digest:
        raise OCRAdapterError("Saved OCR job belongs to a different source.png; refusing to reuse or resubmit.")
    credential = resolve_credential()
    token = credential.value
    if not token:
        raise OCRAdapterError("No PaddleOCR credential is configured; no OCR request was sent.")
    job_id = state.get("job_id")
    if not state:
        state = {"schema_version": 1, "source_sha256": digest, "status": "submission_started",
                 "credential_source": credential.source}
        _save_state(state_path, state)
        try:
            with source.open("rb") as image:
                response = requests.post(
                    JOB_URL, headers={"Authorization": f"bearer {token}"},
                    data={"model": DEFAULT_MODEL,
                         "optionalPayload": json.dumps({"useDocOrientationClassify": False, "useDocUnwarping": False, "useChartRecognition": False})},
                    files={"file": image}, timeout=(5, 60),
                )
            if response.status_code != 200:
                state.update({"status": "submission_rejected", "http_status": response.status_code})
                _save_state(state_path, state)
                raise OCRAdapterError(f"PaddleOCR submission rejected (HTTP {response.status_code}); no retry was made.")
            job_id = str(response.json()["data"]["jobId"])
            state.update({"job_id": job_id, "status": "submitted"})
            _save_state(state_path, state)
        except OCRAdapterError:
            raise
        except Exception as exc:
            state.update({"status": "submission_outcome_unknown", "error_type": type(exc).__name__})
            _save_state(state_path, state)
            raise OCRAdapterError("Submission outcome is unknown; state saved and automatic resubmission is disabled.") from None
    elif not job_id:
        raise OCRAdapterError("A prior submission has no recoverable job id; automatic resubmission is disabled.")
    state_value, raw = _get_job_result(job_id, token)
    state["service_state"] = state_value
    _save_state(state_path, state)
    if state_value != "done":
        raise OCRAdapterError(f"Existing job is {state_value}; rerun later to resume the same job (no new POST).")
    return persist_then_parse(source, out_dir, raw, state, token=token)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="one image named source.png")
    parser.add_argument("--out-dir", type=Path, help="output directory; defaults beside source.png")
    args = parser.parse_args()
    out_dir = args.out_dir or args.source.parent / "ocr-first"
    try:
        result = run_one_page(args.source, out_dir)
    except OCRAdapterError as exc:
        parser.error(str(exc))
    print(json.dumps({"status": "ok", "job_id": result.get("job_id"),
                      "candidates": len(result["candidates"]), "out_dir": str(out_dir.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
