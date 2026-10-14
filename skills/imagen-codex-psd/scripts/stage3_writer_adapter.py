#!/usr/bin/env python3
"""Build and structurally verify top-origin native text layers.

The cache uses the reviewed layout box without an adapter-applied vertical inset.
Photoshop text stores one ParagraphRun per CR-delimited paragraph, with
SpaceBefore=0. Structural alignment is not a Photoshop edit/reflow acceptance test.
"""
from __future__ import annotations

import copy
import argparse
import json
import math
from pathlib import Path
from types import ModuleType
from typing import Any

import psd_writer


class WriterAdapterError(ValueError):
    pass


def load_writer() -> ModuleType:
    required = ("_text_image", "_text_engine_data", "build_psd")
    if any(not hasattr(psd_writer, name) for name in required):
        raise WriterAdapterError("Bundled TySh writer is missing a required text hook")
    return psd_writer


def _paragraph_run_lengths_utf16(editor_text: str) -> list[int]:
    """Return one UTF-16 range per native paragraph, including each CR terminator."""
    if not isinstance(editor_text, str) or not editor_text.endswith("\r") or "\n" in editor_text:
        raise WriterAdapterError("Native Editor.Text must use CR separators and end with a CR")
    paragraphs = editor_text.split("\r")
    if paragraphs[-1] != "":
        raise WriterAdapterError("Native Editor.Text has malformed paragraph termination")
    lengths = [len((paragraph + "\r").encode("utf-16-be")) // 2
               for paragraph in paragraphs[:-1]]
    total_units = len(editor_text.encode("utf-16-be")) // 2
    if not lengths or sum(lengths) != total_units or any(length <= 0 for length in lengths):
        raise WriterAdapterError("ParagraphRun ranges do not cover native Editor.Text exactly once")
    return lengths


def _split_paragraph_engine_runs(engine: Any, source_text: str) -> list[int]:
    """Split the writer's default paragraph sheet at every native CR boundary."""
    from psd_tools.psd import engine_data

    engine_dict = engine["EngineDict"]
    editor_text = str(engine_dict["Editor"]["Text"].value)
    expected_editor_text = str(source_text).replace("\r\n", "\r").replace("\n", "\r") + "\r"
    if editor_text != expected_editor_text:
        raise WriterAdapterError("Native Editor.Text differs from the text used for paragraph ranges")
    lengths = _paragraph_run_lengths_utf16(editor_text)
    paragraph = engine_dict["ParagraphRun"]
    templates = list(paragraph["RunArray"])
    if len(templates) != 1:
        raise WriterAdapterError("Bundled writer must provide one default ParagraphRun template")
    runs = []
    for _ in lengths:
        run = copy.deepcopy(templates[0])
        run["ParagraphSheet"]["Properties"]["SpaceBefore"] = engine_data.Float(0.0)
        runs.append(run)
    paragraph["RunArray"] = engine_data.List(runs)
    paragraph["RunLengthArray"] = engine_data.List([engine_data.Integer(length) for length in lengths])
    return lengths


def install_layout_hooks(writer: ModuleType) -> dict[str, dict[str, Any]]:
    """Record the layout box used by the cache; do not synthesize a text inset."""
    original_text_image = getattr(writer, "_stage3_original_text_image", writer._text_image)
    original_text_engine_data = getattr(writer, "_stage3_original_text_engine_data", writer._text_engine_data)
    writer._stage3_original_text_image = original_text_image
    writer._stage3_original_text_engine_data = original_text_engine_data
    metrics: dict[str, dict[str, Any]] = {}

    def anchored_text_image(item: dict):
        item_id = str(item.get("id", ""))
        mode = item.get("vertical_alignment", "top")
        if mode != "top":
            raise WriterAdapterError(
                f"{item_id}: vertical_alignment={mode!r} is unsupported; native top-origin layout only"
            )
        box = item.get("layout_box_px") or item.get("source_box_px")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise WriterAdapterError(f"{item_id}: layout_box_px is required for native/cache anchoring")
        image, style = original_text_image(item)
        layout_box = [int(value) for value in box]
        metrics[item_id] = {
            "vertical_alignment": "top",
            "layout_box_px": layout_box,
            "layout_origin_px": layout_box[:2],
            "cache_frame_size_px": list(image.size),
            "rotation_deg": float(item.get("rotation_deg", 0.0)),
        }
        style.update({"text_item_id": item_id, "vertical_alignment": "top"})
        return image, style

    writer._text_image = anchored_text_image

    def paragraph_ranges_engine_data(text_meta: dict, box: list[int]):
        engine = original_text_engine_data(text_meta, box)
        _split_paragraph_engine_runs(engine, str(text_meta["text"]))
        return engine

    writer._text_engine_data = paragraph_ranges_engine_data
    return metrics


def validate_and_record(output_dir: Path, page: dict, metrics: dict[str, dict[str, Any]],
                        manifest: dict) -> dict:
    from psd_tools import PSDImage

    output_dir = output_dir.resolve()
    psd = PSDImage.open(output_dir / manifest["psd"])
    by_name = {str(layer.name).rstrip("\x00"): layer for layer in psd.descendants()
               if getattr(layer, "kind", None) != "group"}
    expected = {item["id"]: item for item in page["text_inventory"]}
    if set(metrics) != set(expected):
        raise WriterAdapterError("Writer did not report every text layout box")

    validation: dict[str, dict[str, Any]] = {}
    for text_id, item in expected.items():
        record = metrics[text_id]
        layer = by_name.get(f"Text - {text_id}")
        if layer is None or layer.kind != "type":
            raise WriterAdapterError(f"Missing native text layer: {text_id}")
        if item.get("vertical_alignment", "top") != "top":
            raise WriterAdapterError(f"Unsupported vertical alignment reached writer: {text_id}")

        box = item["layout_box_px"]
        if list(box) != record["layout_box_px"]:
            raise WriterAdapterError(f"Layout box changed after preview creation: {text_id}")
        tysh = layer._record.tagged_blocks.get_data(b"TySh")
        expected_bounds = (box[0], box[1], box[0] + box[2], box[1] + box[3])
        if tysh is None or (tysh.left, tysh.top, tysh.right, tysh.bottom) != expected_bounds:
            raise WriterAdapterError(f"TySh layout box changed: {text_id}")

        angle = float(item["rotation_deg"])
        cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        cx, cy = box[2] / 2.0, box[3] / 2.0
        frame_center = (box[0] + cx, box[1] + cy)
        anchor = item.get("rotation_anchor_px")
        if anchor is None:
            target_center = frame_center
        else:
            ax, ay = anchor
            dx, dy = frame_center[0] - ax, frame_center[1] - ay
            target_center = (ax + cosine * dx - sine * dy, ay + sine * dx + cosine * dy)
        expected_transform = (cosine, sine, -sine, cosine,
                              target_center[0] - cosine * cx + sine * cy,
                              target_center[1] - sine * cx - cosine * cy)
        actual_transform = tuple(float(value) for value in tysh.transform)
        if len(actual_transform) != 6 or any(abs(a - b) > 1e-6 for a, b in zip(actual_transform, expected_transform)):
            raise WriterAdapterError(f"TySh transform changed: {text_id}")

        paragraph = layer.engine_dict["ParagraphRun"]
        paragraph_runs = list(paragraph["RunArray"])
        run_spaces = [float(run["ParagraphSheet"]["Properties"]["SpaceBefore"])
                      for run in paragraph_runs]
        editor = str(layer.engine_dict["Editor"]["Text"].value)
        expected_editor = str(item["text"]).replace("\r\n", "\r").replace("\n", "\r") + "\r"
        if editor != expected_editor:
            raise WriterAdapterError(f"Native Editor.Text changed: {text_id}")
        expected_run_lengths = _paragraph_run_lengths_utf16(editor)
        run_lengths = [int(value) for value in paragraph["RunLengthArray"]]
        if (len(paragraph_runs) != len(expected_run_lengths)
                or run_spaces != [0.0] * len(expected_run_lengths)
                or run_lengths != expected_run_lengths):
            raise WriterAdapterError(f"Expected zero-inset ParagraphRuns covering each native paragraph: {text_id}")

        cached = layer.topil()
        if cached is None or cached.size != tuple(record["cache_frame_size_px"]):
            raise WriterAdapterError(f"Cached text frame changed: {text_id}")
        cache_width, cache_height = record["cache_frame_size_px"]
        shift_x, shift_y = target_center[0] - frame_center[0], target_center[1] - frame_center[1]
        left = int(round(box[0] + (box[2] - cache_width) / 2.0 + shift_x))
        top = int(round(box[1] + (box[3] - cache_height) / 2.0 + shift_y))
        expected_cache_bounds = (left, top, left + cache_width, top + cache_height)
        if tuple(int(value) for value in layer.bbox) != expected_cache_bounds:
            raise WriterAdapterError(f"Cached layer box moved: {text_id}")
        cache_center = (left + cache_width / 2.0, top + cache_height / 2.0)
        if any(abs(a - b) > 0.51 for a, b in zip(cache_center, target_center)):
            raise WriterAdapterError(f"Cached frame and transformed TySh frame centers differ: {text_id}")

        normalized = lambda value: str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        if normalized(layer.text) != normalized(item["text"]):
            raise WriterAdapterError(f"Native text changed: {text_id}")
        validation[text_id] = {
            **record,
            "paragraph_run_lengths_utf16": run_lengths,
            "paragraph_space_before_px": run_spaces,
            "tysh_transform": list(actual_transform),
            "tysh_bounds": list(expected_bounds),
            "cached_frame_bounds": list(expected_cache_bounds),
            "cache_and_tysh_share_layout_frame_center": True,
        }

    manifest["layout_anchor_validation"] = {
        "status": "STRUCTURAL_ONLY_PASS",
        "vertical_alignment_supported": ["top"],
        "vertical_centering": "UNSUPPORTED",
        "native_layout": "one ParagraphRun per CR-delimited paragraph, including CR in UTF-16 range; SpaceBefore=0; TySh bounds equal layout_box_px",
        "cached_layout": "original text raster; no adapter-applied vertical inset; frame placed on TySh layout center",
        "photoshop_manual_check": "PENDING_MANUAL_PHOTOSHOP",
        "items": validation,
    }
    (output_dir / "stage3_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                                       encoding="utf-8")
    return manifest


def build(page_path: Path, out_dir: Path) -> dict:
    page = json.loads(page_path.read_text(encoding="utf-8"))
    writer = load_writer()
    metrics = install_layout_hooks(writer)
    manifest = writer.build_psd(page_path, out_dir)
    return validate_and_record(out_dir, page, metrics, manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page_rebuild_json")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build(Path(args.page_rebuild_json), Path(args.out_dir))
        print(json.dumps({"status": manifest["layout_anchor_validation"]["status"],
                          "psd": str(Path(args.out_dir).resolve() / manifest["psd"]),
                          "vertical_alignment_supported": ["top"],
                          "photoshop_manual_check": "PENDING_MANUAL_PHOTOSHOP"}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
