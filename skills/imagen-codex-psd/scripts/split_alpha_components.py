#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def parse_names(value):
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


NEIGHBORS = {
    4: ((1, 0), (-1, 0), (0, 1), (0, -1)),
    8: ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)),
}
MAX_SHEET_OBJECTS = 9


def plan_sheet_groups(objects):
    """Preserve inventory order and split missing objects into sparse boards of at most 9."""
    if not isinstance(objects, list):
        raise ValueError("objects must be an inventory list")
    seen = set()
    normalized = []
    for item in objects:
        if not isinstance(item, dict):
            raise ValueError("each inventory item must be an object")
        object_id = item.get("id")
        if not isinstance(object_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", object_id):
            raise ValueError("inventory IDs must be stable safe IDs")
        if object_id in seen:
            raise ValueError(f"duplicate inventory ID: {object_id}")
        seen.add(object_id)
        source_box = item.get("source_bbox_px")
        if (not isinstance(source_box, list) or len(source_box) != 4
                or any(type(value) is not int for value in source_box)
                or source_box[0] < 0 or source_box[1] < 0
                or source_box[2] <= 0 or source_box[3] <= 0):
            raise ValueError(f"{object_id}: source_bbox_px must be a positive integer box")
        normalized.append({key: item[key] for key in ("id", "description", "source_bbox_px") if key in item})
    batches = []
    for offset in range(0, len(normalized), MAX_SHEET_OBJECTS):
        group = normalized[offset:offset + MAX_SHEET_OBJECTS]
        batches.append({"sheet_id": f"sheet-{len(batches) + 1:02d}",
                        "layout": "sparse_3x3_row_major", "objects": group})
    return batches


def foreground_mask(alpha, threshold, close_radius):
    mask = alpha.point(lambda p: 255 if p > threshold else 0, mode="L")
    if close_radius <= 0:
        return mask
    size = close_radius * 2 + 1
    return mask.filter(ImageFilter.MaxFilter(size)).filter(ImageFilter.MinFilter(size))


def component_boxes(mask, min_area, connectivity):
    width, height = mask.size
    pixels = mask.load()
    seen = bytearray(width * height)
    components = []
    neighbors = NEIGHBORS[connectivity]

    for y in range(height):
        for x in range(width):
            idx = y * width + x
            if seen[idx] or pixels[x, y] == 0:
                continue

            stack = [(x, y)]
            seen[idx] = 1
            min_x = max_x = x
            min_y = max_y = y
            area = 0

            while stack:
                cx, cy = stack.pop()
                area += 1
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)

                for dx, dy in neighbors:
                    nx = cx + dx
                    ny = cy + dy
                    if 0 <= nx < width and 0 <= ny < height:
                        nidx = ny * width + nx
                        if not seen[nidx] and pixels[nx, ny] != 0:
                            seen[nidx] = 1
                            stack.append((nx, ny))

            if area >= min_area:
                components.append({"area": area, "box": [min_x, min_y, max_x + 1, max_y + 1]})

    return components


def gap_between(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    dx = max(0, max(ax1, bx1) - min(ax2, bx2))
    dy = max(0, max(ay1, by1) - min(ay2, by2))
    return max(dx, dy)


def union_box(a, b):
    return [
        min(a[0], b[0]),
        min(a[1], b[1]),
        max(a[2], b[2]),
        max(a[3], b[3]),
    ]


def box_area(box):
    return max(1, box[2] - box[0]) * max(1, box[3] - box[1])


def should_merge(a, b, max_gap, max_union_growth):
    if gap_between(a["box"], b["box"]) > max_gap:
        return False
    union = union_box(a["box"], b["box"])
    separate_area = box_area(a["box"]) + box_area(b["box"])
    return box_area(union) / max(1, separate_area) <= max_union_growth


def merge_fragments(components, max_gap, max_union_growth):
    if max_gap <= 0:
        return components
    merged = [dict(component, merged_count=1) for component in components]
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                if not should_merge(merged[i], merged[j], max_gap, max_union_growth):
                    continue
                merged[i] = {
                    "area": merged[i]["area"] + merged[j]["area"],
                    "box": union_box(merged[i]["box"], merged[j]["box"]),
                    "merged_count": merged[i]["merged_count"] + merged[j]["merged_count"],
                }
                del merged[j]
                changed = True
                break
            if changed:
                break
    return merged


def sort_components(components, mode):
    if mode == "area":
        return sorted(components, key=lambda item: item["area"], reverse=True)
    if mode == "y":
        return sorted(components, key=lambda item: (item["box"][1], item["box"][0]))
    return sorted(components, key=lambda item: (item["box"][0], item["box"][1]))


def extract_component_asset(image, box, pad, square):
    width, height = image.size
    left, top, right, bottom = box
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(width, right + pad)
    bottom = min(height, bottom + pad)
    component_image = image.crop((left, top, right, bottom))
    if not square:
        return component_image, [left, top, right, bottom]

    side = max(component_image.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.alpha_composite(
        component_image,
        ((side - component_image.size[0]) // 2, (side - component_image.size[1]) // 2),
    )
    return canvas, [left, top, right, bottom]


def write_contact_sheet(items, out_path):
    if not items:
        return
    thumb = 180
    label_h = 28
    cols = min(5, len(items))
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * thumb, rows * (thumb + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    for index, item in enumerate(items):
        row, col = divmod(index, cols)
        preview = item["image"].copy()
        preview.thumbnail((thumb - 16, thumb - 16))
        x = col * thumb + (thumb - preview.width) // 2
        y = row * (thumb + label_h) + (thumb - preview.height) // 2
        checker = Image.new("RGB", preview.size, "#f4f4f4")
        checker.paste(preview, mask=preview.getchannel("A"))
        sheet.paste(checker, (x, y))
        draw.text((col * thumb + 8, row * (thumb + label_h) + thumb), item["name"], fill="black")
    sheet.save(out_path)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split_cells(image, source_path, cell_map_path, out_dir):
    cell_map_path = Path(cell_map_path)
    cell_map = json.loads(cell_map_path.read_text(encoding="utf-8"))
    source_hash = sha256_file(source_path)
    if cell_map.get("schema_version") != 1 or cell_map.get("processed_sheet_sha256") != source_hash:
        raise SystemExit("Cell map must bind the processed alpha sheet SHA-256.")
    generated_path = Path(cell_map.get("generated_sheet_path", ""))
    if not generated_path.is_absolute():
        generated_path = cell_map_path.parent / generated_path
    generated_path = generated_path.resolve()
    if not generated_path.is_file() or sha256_file(generated_path) != cell_map.get("generated_sheet_sha256"):
        raise SystemExit("Cell map must bind the original generated sheet path and SHA-256.")
    cells = cell_map.get("cells")
    if not isinstance(cells, list) or not cells or len(cells) > 9:
        raise SystemExit("Cell map must list 1 to 9 required objects per sparse sheet.")
    width, height = image.size
    names, boxes, outputs, contact_items = set(), [], [], []
    for cell in cells:
        if not isinstance(cell, dict):
            raise SystemExit("Each cell-map entry must be an object.")
        name, raw_box = cell.get("id"), cell.get("bbox_px")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) or name in names:
            raise SystemExit("Cell IDs must be unique safe IDs.")
        if (not isinstance(raw_box, list) or len(raw_box) != 4
                or any(type(v) is not int for v in raw_box)):
            raise SystemExit(f"{name}: cell bbox must be integer [x, y, width, height].")
        x, y, w, h = raw_box
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise SystemExit(f"{name}: cell bbox exceeds the processed sheet.")
        for other_id, (ox, oy, ow, oh) in boxes:
            if x < ox + ow and ox < x + w and y < oy + oh and oy < y + h:
                raise SystemExit(f"{name}: cell overlaps mapped object {other_id}.")
        names.add(name)
        boxes.append((name, (x, y, w, h)))

        cell_image = image.crop((x, y, x + w, y + h)).convert("RGBA")
        visible = cell_image.getchannel("A").getbbox()
        if visible is None:
            raise SystemExit(f"{name}: mapped cell contains no visible alpha object.")
        vx1, vy1, vx2, vy2 = visible
        if vx1 == 0 or vy1 == 0 or vx2 == w or vy2 == h:
            raise SystemExit(f"{name}: object touches its cell edge; the sheet lacks separation margin.")
        # Keep the cell's transparent margin so every extracted PNG has a real alpha border.
        # The visible component bbox is recorded separately for source-sheet reconciliation.
        asset = cell_image
        asset_path = Path(out_dir) / f"{name}.png"
        if asset_path.exists():
            raise SystemExit(f"Refusing to overwrite extracted object: {asset_path}")
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset.save(asset_path)
        component_box = [x + vx1, y + vy1, vx2 - vx1, vy2 - vy1]
        outputs.append({"id": name, "cell_bbox_px": [x, y, w, h],
                        "component_bbox_px": component_box,
                        "path": str(asset_path.resolve()), "sha256": sha256_file(asset_path),
                        "size_px": list(asset.size)})
        contact_items.append({"name": name, "image": asset})
    return {"schema_version": 1, "generated_sheet_path": str(generated_path),
            "generated_sheet_sha256": cell_map["generated_sheet_sha256"],
            "source_path": str(Path(source_path).resolve()),
            "source_sha256": source_hash, "cell_map_path": str(cell_map_path.resolve()),
            "cell_map_sha256": sha256_file(cell_map_path), "assets": outputs}, contact_items


def main():
    parser = argparse.ArgumentParser(description="Split a transparent asset sheet into component PNG files.")
    parser.add_argument("--input", required=True, help="RGBA asset sheet after chroma-key removal")
    parser.add_argument("--out-dir", required=True, help="Directory for split PNG assets")
    parser.add_argument("--names", help="Comma-separated output names in sorted component order")
    parser.add_argument("--sort", choices=["x", "y", "area"], default="x")
    parser.add_argument("--threshold", type=int, default=20, help="Alpha threshold for foreground pixels")
    parser.add_argument("--min-area", type=int, default=1000, help="Minimum connected area to keep")
    parser.add_argument("--connectivity", type=int, choices=[4, 8], default=8)
    parser.add_argument("--close-radius", type=int, default=3, help="Small morphology close radius for broken strokes")
    parser.add_argument("--merge-gap", type=int, default=18, help="Merge nearby fragments within this pixel gap; 0 disables")
    parser.add_argument("--merge-union-growth", type=float, default=2.4, help="Maximum union-box growth allowed when merging fragments")
    parser.add_argument("--pad", type=int, default=24)
    parser.add_argument("--limit", type=int, help="Maximum number of components to write")
    parser.add_argument("--square", action="store_true", help="Place each extracted asset on a transparent square canvas")
    parser.add_argument("--manifest", help="Optional JSON report path for component boxes and outputs")
    parser.add_argument("--contact-sheet", help="Optional contact sheet image for visual QA")
    parser.add_argument("--cell-map", help="Reviewed JSON mapping object IDs to non-overlapping cell bboxes in the processed alpha sheet")
    args = parser.parse_args()

    src = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    image = Image.open(src).convert("RGBA")
    if args.cell_map:
        if args.names or args.limit:
            raise SystemExit("--cell-map cannot be combined with component-order --names or --limit.")
        report, contact_items = split_cells(image, src, args.cell_map, args.out_dir)
        for item in report["assets"]:
            print(f"{item['id']}: cell={item['cell_bbox_px']} component={item['component_bbox_px']} sha256={item['sha256']}")
        if args.manifest:
            Path(args.manifest).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.contact_sheet:
            write_contact_sheet([{"name": item["name"], "image": item["image"]} for item in contact_items],
                                Path(args.contact_sheet))
        return
    mask = foreground_mask(image.getchannel("A"), args.threshold, args.close_radius)
    components = component_boxes(mask, args.min_area, args.connectivity)
    components = merge_fragments(components, args.merge_gap, args.merge_union_growth)
    components = sort_components(components, args.sort)
    if args.limit:
        components = components[: args.limit]

    names = parse_names(args.names)
    if names and len(names) != len(components):
        raise SystemExit(f"--names supplied {len(names)} names, but detected {len(components)} components")

    outputs = []
    contact_items = []
    for index, component in enumerate(components, start=1):
        name = names[index - 1] if names else f"asset_{index:02d}.png"
        if not name.lower().endswith(".png"):
            name += ".png"
        component_image, padded_box = extract_component_asset(image, component["box"], args.pad, args.square)
        out_path = out_dir / name
        component_image.save(out_path)
        entry = {
            "path": str(out_path),
            "source": str(src),
            "box": component["box"],
            "padded_box": padded_box,
            "area": component["area"],
            "merged_count": component.get("merged_count", 1),
            "size": list(component_image.size),
        }
        outputs.append(entry)
        contact_items.append({"name": name, "image": component_image})
        print(f"{name}: box={component['box']} area={component['area']} size={component_image.size}")

    if args.manifest:
        Path(args.manifest).write_text(json.dumps({"source": str(src), "assets": outputs}, ensure_ascii=False, indent=2))
    if args.contact_sheet:
        write_contact_sheet(contact_items, Path(args.contact_sheet))


if __name__ == "__main__":
    main()
