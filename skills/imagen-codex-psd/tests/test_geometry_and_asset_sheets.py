import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ocr_first_adapter  # noqa: E402
import prepare_stage3  # noqa: E402
import split_alpha_components  # noqa: E402
import stage3_writer_adapter  # noqa: E402
import text_background  # noqa: E402
import text_first  # noqa: E402


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class SourceGeometryTests(unittest.TestCase):
    def test_quad_suggests_angle_but_full_contour_stays_reference_only(self):
        quad = {"block_polygon_points": [[0, 20], [100, 15], [100, 35], [0, 40]]}
        result = ocr_first_adapter._polygon_geometry(quad, 1, 1, 120, 60)
        self.assertEqual(result["kind"], "quadrilateral")
        self.assertEqual(len(result["points_px"]), 4)
        self.assertLess(result["suggested_rotation_deg"], 0)

        contour = {"block_polygon_points": [[i, 10 + (i % 3)] for i in range(11)]}
        result = ocr_first_adapter._polygon_geometry(contour, 1, 1, 120, 60)
        self.assertEqual(result["kind"], "polygon")
        self.assertEqual(len(result["points_px"]), 11)
        self.assertIsNone(result["suggested_rotation_deg"])
        with self.assertRaisesRegex(text_first.PlanError, "finite numbers"):
            text_first._validate_source_geometry(
                {"kind": "quadrilateral", "points_px": [[0, 0], [10, 0], [10, 10], [0, float("nan")]]},
                120, 60, "P01")
        with self.assertRaisesRegex(prepare_stage3.Stage3InputError, "finite"):
            prepare_stage3.point([float("nan"), 5], (120, 60), "anchor")


class AssetSheetBatchTests(unittest.TestCase):
    def test_inventory_grouping_counts_and_order(self):
        for count, expected in ((0, []), (1, [1]), (4, [4]), (9, [9]), (10, [9, 1])):
            objects = [{"id": f"OBJ_{i + 1:02d}", "source_bbox_px": [i * 5, 2, 4, 4],
                        "description": f"object {i + 1}"} for i in range(count)]
            groups = split_alpha_components.plan_sheet_groups(objects)
            self.assertEqual([len(group["objects"]) for group in groups], expected)
            self.assertEqual([obj["id"] for group in groups for obj in group["objects"]],
                             [obj["id"] for obj in objects])
            self.assertTrue(all(group["layout"] == "sparse_3x3_row_major" for group in groups))

    def test_synthetic_sheet_splits_two_independent_assets_and_binds_each_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generated = root / "generated.png"
            generated_image = Image.new("RGB", (80, 40), "#00ffff")
            draw = ImageDraw.Draw(generated_image)
            draw.ellipse((8, 8, 23, 31), fill="#bd4b13")
            draw.rectangle((52, 10, 65, 29), fill="#265bb5")
            generated_image.save(generated)

            processed = root / "processed.png"
            processed_image = Image.new("RGBA", (80, 40), (0, 0, 0, 0))
            processed_image.alpha_composite(generated_image.convert("RGBA"), (0, 0))
            alpha = Image.new("L", (80, 40), 0)
            alpha_draw = ImageDraw.Draw(alpha)
            alpha_draw.ellipse((8, 8, 23, 31), fill=255)
            alpha_draw.rectangle((52, 10, 65, 29), fill=255)
            processed_image.putalpha(alpha)
            processed_image.save(processed)

            cell_map_path = root / "cell-map.json"
            write_json(cell_map_path, {
                "schema_version": 1,
                "generated_sheet_path": str(generated),
                "generated_sheet_sha256": sha256(generated),
                "processed_sheet_sha256": sha256(processed),
                "cells": [
                    {"id": "OBJ_A", "bbox_px": [0, 0, 40, 40]},
                    {"id": "OBJ_B", "bbox_px": [40, 0, 40, 40]},
                ],
            })
            assets_dir = root / "split-assets"
            report, _ = split_alpha_components.split_cells(
                Image.open(processed).convert("RGBA"), processed, cell_map_path, assets_dir)
            split_manifest = root / "split-report.json"
            write_json(split_manifest, report)

            source = root / "source.png"
            base = root / "clean-base.png"
            Image.new("RGB", (64, 48), "#f0f0f0").save(source)
            Image.new("RGB", (64, 48), "#dedede").save(base)
            records = []
            for entry, source_box in zip(report["assets"], ([3, 5, 12, 12], [24, 5, 12, 12])):
                records.append({
                    "id": entry["id"], "type": "missing_from_clean_base", "reason": "synthetic behavior test",
                    "source_bbox_px": source_box,
                    "alpha_asset_path": entry["path"], "alpha_asset_sha256": entry["sha256"],
                    "asset_sheet_ref": {
                        "sheet_id": "batch-01", "cell_id": entry["id"],
                        "generated_sheet_path": str(generated), "generated_sheet_sha256": sha256(generated),
                        "processed_sheet_path": str(processed), "processed_sheet_sha256": sha256(processed),
                        "cell_map_path": str(cell_map_path), "cell_map_sha256": sha256(cell_map_path),
                        "split_manifest_path": str(split_manifest), "split_manifest_sha256": sha256(split_manifest),
                        "cell_bbox_px": entry["cell_bbox_px"], "component_bbox_px": entry["component_bbox_px"],
                    },
                    "visual_review": {"status": "approved", "reviewer": "synthetic-test", "note": "fixture only"},
                    "placement": {"mode": "contain", "note": "fixture only"},
                })
            exception_path = root / "exceptions.json"
            write_json(exception_path, {
                "schema_version": 1, "status": "reviewed", "backend": {"type": "synthetic-test"},
                "inputs": {"identity_source": {"path": str(source), "sha256": sha256(source)}},
                "clean_base": {"path": str(base), "sha256": sha256(base)},
                "imagegen_output": {"workspace_asset_sheet": str(generated),
                                    "workspace_sha256": sha256(generated), "generated_sha256": sha256(generated)},
                "processing": {}, "foreground_exceptions": records,
            })
            validated = text_background._load_exceptions(
                exception_path, sha256(source), sha256(base), ["OBJ_A", "OBJ_B"], (64, 48))
            self.assertEqual([obj["id"] for obj in validated], ["OBJ_A", "OBJ_B"])
            self.assertEqual(len({obj["input_asset_path"] for obj in validated}), 2)
            self.assertEqual(len({obj["asset_sha256"] for obj in validated}), 2)
            self.assertEqual([obj["source_bbox_px"] for obj in validated], [[3, 5, 12, 12], [24, 5, 12, 12]])
            self.assertEqual({obj["asset_sheet_ref"]["generated_sheet_sha256"] for obj in validated},
                             {sha256(generated)})
            self.assertTrue(all(obj["asset_sheet_ref"]["sheet_id"] == "batch-01" for obj in validated))
            self.assertNotIn("alpha_asset_path", validated[0]["provenance"]["processing"])


class SharedRotationWriterTests(unittest.TestCase):
    def test_group_anchor_matches_native_transform_cache_and_shared_baseline(self):
        from psd_tools import PSDImage

        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            stage2 = run / "stage2"
            stage2.mkdir(parents=True)
            source = run / "source.png"
            base = stage2 / "clean-base.png"
            Image.new("RGB", (240, 120), "#ffffff").save(source)
            Image.new("RGB", (240, 120), "#d9d9d9").save(base)
            source_hash, base_hash = sha256(source), sha256(base)
            items = []
            for item_id, phrase, x, color, weight in (
                ("BLACK_PART", "标题", 24, [30, 30, 30, 255], "Bold"),
                ("ORANGE_PART", "重点", 106, [228, 70, 20, 255], "Bold"),
            ):
                items.append({
                    "id": item_id, "text": phrase,
                    "source_box_px": [x - 8, 12, 98, 56], "layout_box_px": [x, 10, 82, 62],
                    "font_weight": weight, "font_size_pt": 22, "color_rgba": color,
                    "alignment": "center", "vertical_alignment": "top",
                    "auto_leading": True, "leading_pt": 0, "tracking": 0,
                    "rotation_deg": -5.0, "rotation_anchor_px": [105.0, 41.0],
                    "rotation_group_id": "one-baseline",
                })
            page = {
                "schema_version": 1, "status": "ready_for_stage_3",
                "source": {"source_png_path": "../source.png", "source_png_sha256": source_hash,
                           "size_px": {"width": 240, "height": 120}},
                "background": {"status": "ready", "decision": "imported_clean_base",
                               "path": "clean-base.png", "sha256": base_hash},
                "foreground_assets": [], "text_inventory": items,
            }
            page_path = stage2 / "page_rebuild.json"
            write_json(page_path, page)
            output = run / "output"
            manifest = stage3_writer_adapter.build(page_path, output)
            psd = PSDImage.open(output / manifest["psd"])
            layers = {str(layer.name).rstrip("\x00"): layer for layer in psd.descendants()
                      if getattr(layer, "kind", None) != "group"}
            centers = []
            dominant_colors = []
            for item in items:
                layer = layers[f"Text - {item['id']}"]
                tysh = layer._record.tagged_blocks.get_data(b"TySh")
                self.assertEqual((tysh.left, tysh.top, tysh.right, tysh.bottom),
                                 (item["layout_box_px"][0], item["layout_box_px"][1],
                                  item["layout_box_px"][0] + item["layout_box_px"][2],
                                  item["layout_box_px"][1] + item["layout_box_px"][3]))
                a, b, c, d, tx, ty = [float(value) for value in tysh.transform]
                box = item["layout_box_px"]
                # TySh translation is already in canvas coordinates; its local
                # frame center must not receive the layout-box origin again.
                native_center = (a * box[2] / 2 + c * box[3] / 2 + tx,
                                 b * box[2] / 2 + d * box[3] / 2 + ty)
                left, top, right, bottom = [int(value) for value in layer.bbox]
                cache_center = ((left + right) / 2, (top + bottom) / 2)
                self.assertLessEqual(math.dist(native_center, cache_center), 1.0)
                angle = math.radians(item["rotation_deg"])
                anchor_x, anchor_y = item["rotation_anchor_px"]
                normal_projection = (-math.sin(angle) * (native_center[0] - anchor_x)
                                     + math.cos(angle) * (native_center[1] - anchor_y))
                centers.append(normal_projection)
                cache = layer.topil().convert("RGBA")
                alpha_box = cache.getchannel("A").getbbox()
                self.assertIsNotNone(alpha_box)
                pixels, visible = cache.load(), {}
                for y in range(alpha_box[1], alpha_box[3]):
                    for x in range(alpha_box[0], alpha_box[2]):
                        pixel = pixels[x, y]
                        if pixel[3] > 240:
                            visible[pixel[:3]] = visible.get(pixel[:3], 0) + 1
                self.assertTrue(visible)
                dominant_colors.append(max(visible, key=visible.get))
            self.assertLessEqual(abs(centers[0] - centers[1]), 0.05)
            self.assertNotEqual(dominant_colors[0], dominant_colors[1])
            self.assertEqual(manifest["layout_anchor_validation"]["status"], "STRUCTURAL_ONLY_PASS")


if __name__ == "__main__":
    unittest.main()
