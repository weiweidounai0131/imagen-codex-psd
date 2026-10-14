#!/usr/bin/env python3
"""Build one simple editable PSD from the lightweight Stage 2 contract.

The small pytoshop layer and Photoshop TySh helpers below are adapted from
ningzimu/image-to-editable-ppt-skill and the local imagen-codex-psd writer;
see THIRD_PARTY_NOTICES.md for the retained MIT notice.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
from collections import OrderedDict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

_BLEND_MODES = {"normal": b"norm", "pass-through": b"pass", "passthrough": b"pass"}
TEXT_FONT_FAMILY = "SourceHanSansCN"
FONT_WEIGHT_OPTIONS = {"ExtraLight", "Light", "Normal", "Regular", "Medium", "Bold", "Heavy"}
FONT_DIR = Path.home() / "Library" / "Fonts"
TEXT_RGBA = (28, 28, 28, 255)

class Stage3Error(ValueError):
    pass

def _fail(code: str, message: str) -> None:
    raise Stage3Error(f"{code}: {message}")

def _positive_box(value: object, label: str) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        _fail("SOURCE_BOX_INVALID", f"{label} needs integer [x, y, width, height]")
    x, y, width, height = value
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        _fail("SOURCE_BOX_INVALID", f"{label} has invalid pixel bounds")
    return [x, y, width, height]

def _contained_path(root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        _fail("STAGE2_PATH_MISSING", f"{label} path is missing")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        _fail("STAGE2_PATH_INVALID", f"{label} must be a file inside the Stage 2 directory")
    return path

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _rgba_pixel_hash(path: Path) -> tuple[tuple[int, int], str]:
    digest = hashlib.sha256()
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        digest.update(f"{rgba.width}x{rgba.height}:".encode("ascii"))
        for top in range(0, rgba.height, 128):
            digest.update(rgba.crop((0, top, rgba.width, min(rgba.height, top + 128))).tobytes())
        return rgba.size, digest.hexdigest()

def _blend(value: object, *, group: bool = False):
    from pytoshop import enums

    name = str(value or ("pass-through" if group else "normal")).lower().replace("_", "-")
    raw = _BLEND_MODES.get(name)
    if raw is None:
        _fail("PSD_BLEND_MODE_INVALID", f"不支持的 blend_mode: {value}")
    try:
        return enums.BlendMode(raw)
    except ValueError:
        _fail("PSD_BLEND_MODE_INVALID", f"不支持的 blend_mode: {value}")
        raise AssertionError

def _channels(image: Image.Image) -> OrderedDict:
    import numpy as np

    array = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    return OrderedDict(
        [
            (0, np.ascontiguousarray(array[:, :, 0])),
            (1, np.ascontiguousarray(array[:, :, 1])),
            (2, np.ascontiguousarray(array[:, :, 2])),
            (-1, np.ascontiguousarray(array[:, :, 3])),
        ]
    )

def _descriptor(items: list[tuple[bytes, Any]], *, cls=None, class_id: bytes = b"null"):
    from psd_tools.psd import descriptor

    klass = cls or descriptor.Descriptor
    return klass(items=items, name="", classID=class_id)

def _engine_color(color: object) -> Any:
    """Encode an RGB(A) color in Photoshop's TypeValues shape."""

    from psd_tools.psd import engine_data

    rgba = _rgba(color)
    values = [rgba[3] / 255.0, rgba[0] / 255.0, rgba[1] / 255.0, rgba[2] / 255.0]
    return _engine_dict(
        {
            "Type": engine_data.Integer(1),
            "Values": engine_data.List([engine_data.Float(value) for value in values]),
        }
    )

def _engine_dict(items: dict):
    from psd_tools.psd import engine_data

    return engine_data.Dict(OrderedDict((engine_data.Property(key), value) for key, value in items.items()))

def _engine_font(name: str, *, script: int = 0, font_type: int = 1) -> Any:
    from psd_tools.psd import engine_data

    return _engine_dict(
        {
            "Name": engine_data.String(name),
            "Script": engine_data.Integer(script),
            "FontType": engine_data.Integer(font_type),
            "Synthetic": engine_data.Integer(0),
        }
    )

def _engine_paragraph_properties(alignment: str) -> Any:
    from psd_tools.psd import engine_data

    justification = {"left": 0, "right": 1, "center": 2}.get(alignment, 0)
    return _engine_dict(
        {
            "Justification": engine_data.Integer(justification),
            "FirstLineIndent": engine_data.Float(0.0),
            "StartIndent": engine_data.Float(0.0),
            "EndIndent": engine_data.Float(0.0),
            "SpaceBefore": engine_data.Float(0.0),
            "SpaceAfter": engine_data.Float(0.0),
            "AutoHyphenate": engine_data.Bool(True),
            "HyphenatedWordSize": engine_data.Integer(6),
            "PreHyphen": engine_data.Integer(2),
            "PostHyphen": engine_data.Integer(2),
            "ConsecutiveHyphens": engine_data.Integer(8),
            "Zone": engine_data.Float(36.0),
            "WordSpacing": engine_data.List([engine_data.Float(value) for value in (0.8, 1.0, 1.33)]),
            "LetterSpacing": engine_data.List([engine_data.Float(value) for value in (0.0, 0.0, 0.0)]),
            "GlyphSpacing": engine_data.List([engine_data.Float(value) for value in (1.0, 1.0, 1.0)]),
            "AutoLeading": engine_data.Float(1.2),
            "LeadingType": engine_data.Integer(0),
            "Hanging": engine_data.Bool(False),
            "Burasagari": engine_data.Bool(False),
            "KinsokuOrder": engine_data.Integer(0),
            "EveryLineComposer": engine_data.Bool(False),
        }
    )

def _engine_resource_dict(font_name: str | list[str], style_data: Any, paragraph_properties: Any) -> Any:
    """Build the canonical resource dictionaries consumed by Photoshop text."""

    from psd_tools.psd import engine_data

    kinsoku_hard = _engine_dict(
        {
            "Name": engine_data.String("PhotoshopKinsokuHard"),
            "NoStart": engine_data.String("、。，．・：；？！ー―’”）〕］｝〉》」』】ヽヾゝゞ々ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ゛゜?!)]},.:;℃℉¢％‰"),
            "NoEnd": engine_data.String("‘“（〔［｛〈《「『【([{￥＄£＠§〒＃"),
            "Keep": engine_data.String("―‥"),
            "Hanging": engine_data.String("、。.,"),
        }
    )
    kinsoku_soft = _engine_dict(
        {
            "Name": engine_data.String("PhotoshopKinsokuSoft"),
            "NoStart": engine_data.String("、。，．・：；？！’”）〕］｝〉》」』】ヽヾゝゞ々"),
            "NoEnd": engine_data.String("‘“（〔［｛〈《「『【"),
            "Keep": engine_data.String("―‥"),
            "Hanging": engine_data.String("、。.,"),
        }
    )
    moji = engine_data.List(
        [_engine_dict({"InternalName": engine_data.String(f"Photoshop6MojiKumiSet{index}")}) for index in range(1, 5)]
    )
    paragraph_sheet = _engine_dict(
        {
            "Name": engine_data.String("Normal RGB"),
            "DefaultStyleSheet": engine_data.Integer(0),
            "Properties": paragraph_properties,
        }
    )
    style_sheet = _engine_dict(
        {
            "Name": engine_data.String("Normal RGB"),
            "StyleSheetData": style_data,
        }
    )
    font_names = [font_name] if isinstance(font_name, str) else list(dict.fromkeys(font_name))
    font_set = engine_data.List([_engine_font("AdobeInvisFont", font_type=0)] +
                                [_engine_font(name, font_type=1) for name in font_names])
    return _engine_dict(
        {
            "KinsokuSet": engine_data.List([kinsoku_hard, kinsoku_soft]),
            "MojiKumiSet": moji,
            "TheNormalStyleSheet": engine_data.Integer(0),
            "TheNormalParagraphSheet": engine_data.Integer(0),
            "ParagraphSheetSet": engine_data.List([paragraph_sheet]),
            "StyleSheetSet": engine_data.List([style_sheet]),
            "FontSet": font_set,
            "SuperscriptSize": engine_data.Float(0.583),
            "SuperscriptPosition": engine_data.Float(0.333),
            "SubscriptSize": engine_data.Float(0.583),
            "SubscriptPosition": engine_data.Float(0.333),
            "SmallCapSize": engine_data.Float(0.7),
        }
    )

def _engine_style_data(text_meta: dict, font_index: int) -> Any:
    from psd_tools.psd import engine_data

    return _engine_dict(
        {
            "Font": engine_data.Integer(font_index),
            "FontSize": engine_data.Float(float(text_meta.get("font_size_pt") or 12.0)),
            "FauxBold": engine_data.Bool(False),
            "FauxItalic": engine_data.Bool(False),
            "AutoLeading": engine_data.Bool(bool(text_meta.get("auto_leading", True))),
            "Leading": engine_data.Float(float(text_meta.get("leading_pt") or 0.0)),
            "HorizontalScale": engine_data.Float(1.0),
            "VerticalScale": engine_data.Float(1.0),
            "Tracking": engine_data.Integer(int(text_meta.get("tracking", 0))),
            "AutoKerning": engine_data.Bool(True),
            "Kerning": engine_data.Integer(0),
            "BaselineShift": engine_data.Float(0.0),
            "FontCaps": engine_data.Integer(0),
            "FontBaseline": engine_data.Integer(0),
            "Underline": engine_data.Bool(False),
            "Strikethrough": engine_data.Bool(False),
            "Ligatures": engine_data.Bool(True),
            "DLigatures": engine_data.Bool(False),
            "BaselineDirection": engine_data.Integer(2),
            "Tsume": engine_data.Float(0.0),
            "StyleRunAlignment": engine_data.Integer(2),
            "Language": engine_data.Integer(0),
            "NoBreak": engine_data.Bool(False),
            "FillColor": _engine_color(text_meta.get("text_color")),
            "StrokeColor": _engine_color((0, 0, 0, 0)),
            "FillFlag": engine_data.Bool(True),
            "StrokeFlag": engine_data.Bool(False),
            "FillFirst": engine_data.Bool(True),
            "YUnderline": engine_data.Integer(1),
            "OutlineWidth": engine_data.Float(1.0),
            "CharacterDirection": engine_data.Integer(0),
            "HindiNumbers": engine_data.Bool(False),
            "Kashida": engine_data.Integer(1),
            "DiacriticPos": engine_data.Integer(2),
        }
    )

def _metadata_bytes(meta: dict) -> bytes:
    """Encode JSON in the PSD ``cust`` metadata descriptor.

    ``cust`` is a known Photoshop metadata key, so psd-tools can parse it on
    reopen without an opaque unknown-tag warning.  The value contains only
    non-secret layer contract data.
    """

    from psd_tools.psd import descriptor

    payload = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    block = descriptor.DescriptorBlock(
        items=[(b"m3b", descriptor.String(payload))], name="", classID=b"m3b"
    )
    return block.tobytes(padding=4)

def _opacity(value: object) -> int:
    if value is None:
        return 255
    try:
        number = float(value)
    except (TypeError, ValueError):
        _fail("PSD_LAYER_OPACITY_INVALID", "图层 opacity 不是有限数值")
    if not math.isfinite(number):
        _fail("PSD_LAYER_OPACITY_INVALID", "图层 opacity 不是有限数值")
    if 0 <= number <= 1:
        number *= 255
    if not 0 <= number <= 255:
        _fail("PSD_LAYER_OPACITY_INVALID", "图层 opacity 必须位于 0..1 或 0..255")
    return int(round(number))

def _pgroup(spec: dict, children: list, group_number: int, meta: dict):
    from pytoshop.user.nested_layers import Group

    return Group(
        name=str(spec.get("name") or spec.get("id")),
        visible=_visible(spec),
        opacity=_opacity(spec.get("opacity")),
        group_id=group_number,
        blend_mode=_blend(spec.get("blend_mode"), group=True),
        layers=children,
        closed=True,
        metadata={b"cust": _metadata_bytes(meta)},
    )

def _pimage(spec: dict, image: Image.Image, *, left: int, top: int, group_number: int, meta: dict):
    from pytoshop import enums
    from pytoshop.user.nested_layers import Image as PImage

    width, height = image.size
    return PImage(
        name=str(spec.get("name") or spec.get("id")),
        visible=_visible(spec),
        opacity=_opacity(spec.get("opacity")),
        group_id=group_number,
        blend_mode=_blend(spec.get("blend_mode")),
        top=int(top),
        left=int(left),
        bottom=int(top + height),
        right=int(left + width),
        channels=_channels(image),
        metadata={b"cust": _metadata_bytes(meta)},
        color_mode=enums.ColorMode.rgb,
    )

def _rgba(value: object, default: tuple[int, int, int, int] = (0, 0, 0, 255)) -> tuple[int, int, int, int]:
    """Accept the small color vocabulary used by inventory records."""

    if isinstance(value, dict):
        lower = {str(key).lower(): val for key, val in value.items()}
        values = [lower.get("r", lower.get("red")), lower.get("g", lower.get("green")), lower.get("b", lower.get("blue"))]
        if all(item is not None for item in values):
            alpha = lower.get("a", lower.get("alpha", 255))
            value = [*values, alpha]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("#"):
            text = text[1:]
        if len(text) in (3, 4):
            text = "".join(ch * 2 for ch in text)
        if len(text) in (6, 8):
            try:
                numbers = tuple(int(text[index : index + 2], 16) for index in range(0, len(text), 2))
                value = numbers
            except ValueError:
                return default
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        try:
            numbers = [float(item) for item in value]
            if all(0 <= item <= 1 for item in numbers):
                numbers = [item * 255 for item in numbers]
            numbers += [255] * (4 - len(numbers))
            return tuple(max(0, min(255, int(round(item)))) for item in numbers)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return default
    return default

def _text_block(layer: dict, text_meta: dict):
    from psd_tools.psd import descriptor, tagged_blocks

    text = str(text_meta["text"])
    box = _positive_box(layer.get("layout_box_px") or layer.get("source_box_px"), str(layer.get("id")))
    engine = _text_engine_data(text_meta, box)
    alpha_box = text_meta.get("ink_box_px")
    if not isinstance(alpha_box, list) or len(alpha_box) != 4:
        alpha_box = [0, 0, box[2], box[3]]
    from psd_tools.terminology import Unit

    bounds = _descriptor(
        [
            (b"Left", _unit_float(0, Unit.Pixels)),
            (b"Top ", _unit_float(0, Unit.Pixels)),
            (b"Rght", _unit_float(box[2], Unit.Pixels)),
            (b"Btom", _unit_float(box[3], Unit.Pixels)),
        ],
        class_id=b"bounds",
    )
    bounding_box = _descriptor(
        [
            (b"Left", _unit_float(alpha_box[0], Unit.Pixels)),
            (b"Top ", _unit_float(alpha_box[1], Unit.Pixels)),
            (b"Rght", _unit_float(alpha_box[0] + alpha_box[2], Unit.Pixels)),
            (b"Btom", _unit_float(alpha_box[1] + alpha_box[3], Unit.Pixels)),
        ],
        class_id=b"boundingBox",
    )
    text_data = descriptor.DescriptorBlock(
        items=[
            (b"Txt ", descriptor.String(text + "\x00")),
            (b"textGridding", descriptor.Enumerated(b"textGridding", b"None")),
            (b"Ornt", descriptor.Enumerated(b"Ornt", b"Hrzn")),
            (b"AntA", descriptor.Enumerated(b"Annt", b"AnSt")),
            (b"bounds", bounds),
            (b"boundingBox", bounding_box),
            (b"TextIndex", descriptor.Integer(0)),
            (b"EngineData", descriptor.RawData(engine.tobytes(indent=None))),
        ],
        name="",
        classID=b"TxLr",
    )
    warp = descriptor.DescriptorBlock(
        items=[
            (b"warpStyle", descriptor.Enumerated(b"warpStyle", b"warpNone")),
            (b"warpValue", descriptor.Double(0.0)),
            (b"warpPerspective", descriptor.Double(0.0)),
            (b"warpPerspectiveOther", descriptor.Double(0.0)),
            (b"warpRotate", descriptor.Enumerated(b"Ornt", b"Hrzn")),
        ],
        name="",
        classID=b"warp",
    )
    try:
        rotation_deg = float(layer.get("rotation") or 0)
    except (TypeError, ValueError):
        rotation_deg = 0.0
    transform = _text_transform(box, rotation_deg, text_meta.get("rotation_anchor_px"))
    return tagged_blocks.TypeToolObjectSetting(
        version=1,
        transform=transform,
        text_version=50,
        text_data=text_data,
        warp_version=1,
        warp=warp,
        left=box[0],
        top=box[1],
        right=box[0] + box[2],
        bottom=box[1] + box[3],
    )

def _text_engine_data(text_meta: dict, box: list[int]) -> Any:
    """Return complete Photoshop EngineData for a single area-text layer.

    A ``TySh`` tag alone is enough for psd-tools to classify a layer as
    ``kind=type`` but is not enough for Photoshop to keep it editable.  The
    canonical Editor/ParagraphRun/StyleRun/Rendered/ResourceDict contract is
    emitted here explicitly, including the Adobe invisible font at index 0.
    """

    from psd_tools.psd import engine_data

    text = str(text_meta["text"])
    editor_text = text.replace("\r\n", "\r").replace("\n", "\r") + "\r"
    alignment = str(text_meta.get("alignment") or "left").lower()
    font_name = str(text_meta.get("font_used") or "MyriadPro-Regular")
    if font_name.casefold() == "arial":
        font_name = "ArialMT"
    units = _utf16_units(editor_text)
    paragraph_properties = _engine_paragraph_properties(alignment)
    raw_style_runs = text_meta.get("style_runs")
    if raw_style_runs is not None:
        if (not isinstance(raw_style_runs, list) or not raw_style_runs
                or "".join(str(run.get("text", "")) for run in raw_style_runs) != text):
            _fail("TEXT_STYLE_RUN_COVERAGE_INVALID", "Style runs must cover the native text exactly once and in order")
        native_font_names = list(dict.fromkeys(str(run["font_used"]) for run in raw_style_runs))
        font_index_by_name = {name: index + 1 for index, name in enumerate(native_font_names)}
        run_style_data = [
            _engine_style_data({**text_meta, **run, "text_color": run.get("text_color", run.get("color_rgba"))},
                               font_index_by_name[str(run["font_used"])])
            for run in raw_style_runs
        ]
        style_data = run_style_data[0]
        run_lengths = [_utf16_units(str(run["text"]).replace("\r\n", "\r").replace("\n", "\r"))
                       for run in raw_style_runs]
        run_lengths[-1] += _utf16_units("\r")
        if sum(run_lengths) != units:
            _fail("TEXT_STYLE_RUN_LENGTH_INVALID", "Style-run UTF-16 lengths do not cover the native editor text")
    else:
        native_font_names = [font_name]
        run_style_data = [_engine_style_data(text_meta, 1)]
        style_data = run_style_data[0]
        run_lengths = [units]
    paragraph_run = _engine_dict(
        {
            "DefaultRunData": _engine_dict(
                {
                    "ParagraphSheet": _engine_dict(
                        {
                            "DefaultStyleSheet": engine_data.Integer(0),
                            "Properties": _engine_dict({}),
                        }
                    ),
                    "Adjustments": _engine_dict(
                        {
                            "Axis": engine_data.List([engine_data.Float(value) for value in (1.0, 0.0, 1.0)]),
                            "XY": engine_data.List([engine_data.Float(value) for value in (0.0, 0.0)]),
                        }
                    ),
                }
            ),
            "RunArray": engine_data.List(
                [
                    _engine_dict(
                        {
                            "ParagraphSheet": _engine_dict(
                                {
                                    "DefaultStyleSheet": engine_data.Integer(0),
                                    "Properties": paragraph_properties,
                                }
                            ),
                            "Adjustments": _engine_dict(
                                {
                                    "Axis": engine_data.List([engine_data.Float(value) for value in (1.0, 0.0, 1.0)]),
                                    "XY": engine_data.List([engine_data.Float(value) for value in (0.0, 0.0)]),
                                }
                            ),
                        }
                    )
                ]
            ),
            "RunLengthArray": engine_data.List([engine_data.Integer(units)]),
            "IsJoinable": engine_data.Integer(1),
        }
    )
    style_run = _engine_dict(
        {
            "DefaultRunData": _engine_dict({"StyleSheet": _engine_dict({"StyleSheetData": _engine_dict({})})}),
            "RunArray": engine_data.List(
                [_engine_dict({"StyleSheet": _engine_dict({"StyleSheetData": data})})
                 for data in run_style_data]
            ),
            "RunLengthArray": engine_data.List([engine_data.Integer(length) for length in run_lengths]),
            "IsJoinable": engine_data.Integer(2),
        }
    )
    zero_color = _engine_color((0, 0, 0, 255))
    rendered_base = _engine_dict(
        {
            "ShapeType": engine_data.Integer(1),
            "BoxBounds": engine_data.List([engine_data.Float(value) for value in (0.0, 0.0, float(box[2]), float(box[3]))]),
            "Base": _engine_dict(
                {
                    "ShapeType": engine_data.Integer(1),
                    "TransformPoint0": engine_data.List([engine_data.Float(1.0), engine_data.Float(0.0)]),
                    "TransformPoint1": engine_data.List([engine_data.Float(0.0), engine_data.Float(1.0)]),
                    "TransformPoint2": engine_data.List([engine_data.Float(0.0), engine_data.Float(0.0)]),
                }
            ),
        }
    )
    rendered_child = _engine_dict(
        {
            "ShapeType": engine_data.Integer(1),
            "Procession": engine_data.Integer(0),
            "Lines": _engine_dict(
                {
                    "WritingDirection": engine_data.Integer(0),
                    "Children": engine_data.List([]),
                }
            ),
            "Cookie": _engine_dict({"Photoshop": rendered_base}),
        }
    )
    engine_dict = _engine_dict(
        {
            "Editor": _engine_dict({"Text": engine_data.String(editor_text)}),
            "ParagraphRun": paragraph_run,
            "StyleRun": style_run,
            "GridInfo": _engine_dict(
                {
                    "GridIsOn": engine_data.Bool(False),
                    "ShowGrid": engine_data.Bool(False),
                    "GridSize": engine_data.Float(18.0),
                    "GridLeading": engine_data.Float(22.0),
                    "GridColor": zero_color,
                    "GridLeadingFillColor": zero_color,
                    "AlignLineHeightToGridFlags": engine_data.Bool(False),
                }
            ),
            "AntiAlias": engine_data.Integer(4),
            "UseFractionalGlyphWidths": engine_data.Bool(True),
            "Rendered": _engine_dict(
                {
                    "Version": engine_data.Integer(1),
                    "Shapes": _engine_dict(
                        {
                            "WritingDirection": engine_data.Integer(0),
                            "Children": engine_data.List([rendered_child]),
                        }
                    ),
                }
            ),
        }
    )
    resources = _engine_resource_dict(native_font_names, style_data, paragraph_properties)
    return engine_data.EngineData(
        OrderedDict(
            [
                (engine_data.Property("EngineDict"), engine_dict),
                (engine_data.Property("ResourceDict"), resources),
                (engine_data.Property("DocumentResources"), _engine_resource_dict(native_font_names, style_data, paragraph_properties)),
            ]
        )
    )

def _unit_float(value: float, unit):
    from psd_tools.psd import descriptor

    return descriptor.UnitFloat(float(value), unit)


def _rotated_center(box: list[int], rotation_deg: float,
                    rotation_anchor_px: list[float] | None = None) -> tuple[float, float]:
    center_x, center_y = box[0] + box[2] / 2.0, box[1] + box[3] / 2.0
    if rotation_anchor_px is None:
        return center_x, center_y
    angle = math.radians(rotation_deg)
    cosine, sine = math.cos(angle), math.sin(angle)
    anchor_x, anchor_y = rotation_anchor_px
    dx, dy = center_x - anchor_x, center_y - anchor_y
    return anchor_x + cosine * dx - sine * dy, anchor_y + sine * dx + cosine * dy


def _text_transform(box: list[int], rotation_deg: float,
                    rotation_anchor_px: list[float] | None = None) -> tuple[float, ...]:
    angle = math.radians(rotation_deg)
    cosine, sine = math.cos(angle), math.sin(angle)
    local_x, local_y = box[2] / 2.0, box[3] / 2.0
    center_x, center_y = _rotated_center(box, rotation_deg, rotation_anchor_px)
    translate_x = center_x - cosine * local_x + sine * local_y
    translate_y = center_y - sine * local_x - cosine * local_y
    return cosine, sine, -sine, cosine, float(translate_x), float(translate_y)

def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-be")) // 2

def _visible(layer: dict) -> bool:
    if layer.get("visible") is False or layer.get("hidden") is True:
        return False
    return True


def _text_preview_bbox(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
                      spacing: int, tracking: int) -> tuple[int, int, int, int]:
    bounds = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing)
    if tracking == 0:
        return bounds
    tracking_px = tracking * font.size / 1000.0
    widths = []
    for line in text.splitlines() or [text]:
        widths.append(sum(font.getlength(char) for char in line) + max(0, len(line) - 1) * tracking_px)
    return (bounds[0], bounds[1], max(bounds[2], int(math.ceil(max(widths, default=0.0)))), bounds[3])


def _text_spacing(font: ImageFont.FreeTypeFont, item: dict) -> int:
    if item.get("auto_leading", True):
        return 0
    return int(round(float(item["leading_pt"]) - font.getmetrics()[0]))


def _resolved_style_runs(item: dict, text: str) -> list[dict] | None:
    raw_runs = item.get("style_runs")
    if raw_runs is None:
        return None
    normalize = lambda value: value.replace("\r\n", "\n").replace("\r", "\n")
    if (not isinstance(raw_runs, list) or not raw_runs
            or "".join(run.get("text", "") for run in raw_runs if isinstance(run, dict)) != text):
        _fail("TEXT_STYLE_RUN_COVERAGE_INVALID", f"Text {item.get('id')} style runs do not cover its text exactly")
    result = []
    for index, run in enumerate(raw_runs):
        if not isinstance(run, dict) or not isinstance(run.get("text"), str) or not run["text"]:
            _fail("TEXT_STYLE_RUN_INVALID", f"Text {item.get('id')} style run {index} has no text")
        weight = str(run.get("font_weight") or "")
        size = run.get("font_size_pt")
        tracking = run.get("tracking", item.get("tracking", 0))
        if weight not in FONT_WEIGHT_OPTIONS or type(size) not in (int, float) or not math.isfinite(float(size)) or size <= 0:
            _fail("TEXT_STYLE_RUN_INVALID", f"Text {item.get('id')} style run {index} needs a supported weight and positive size")
        if type(tracking) is not int:
            _fail("TEXT_STYLE_RUN_INVALID", f"Text {item.get('id')} style run {index} tracking must be an integer")
        if not isinstance(run.get("color_rgba"), (list, tuple)) or len(run["color_rgba"]) not in (3, 4):
            _fail("TEXT_STYLE_RUN_INVALID", f"Text {item.get('id')} style run {index} needs color_rgba")
        color = _rgba(run["color_rgba"])
        font_path = FONT_DIR / f"SourceHanSansCN-{weight}.otf"
        if not font_path.is_file():
            _fail("SOURCE_HAN_FONT_MISSING", f"Required local Source Han Sans CN face is missing: {font_path}")
        pixel_size = max(1, int(round(float(size))))
        try:
            font = ImageFont.truetype(str(font_path), pixel_size)
        except OSError as exc:
            _fail("SOURCE_HAN_FONT_UNREADABLE", f"Cannot load {font_path}: {exc}")
        result.append({
            "text": normalize(run["text"]), "font_weight": weight, "font_used": f"{TEXT_FONT_FAMILY}-{weight}",
            "font_file": font_path.name, "font_size_pt": float(size), "font": font,
            "text_color": color, "color_rgba": list(color), "tracking": tracking,
        })
    if "".join(run["text"] for run in result) != normalize(text):
        _fail("TEXT_STYLE_RUN_COVERAGE_INVALID", f"Text {item.get('id')} style run line breaks do not match its text")
    return result


def _style_run_lines(runs: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = [[]]
    for run in runs:
        parts = run["text"].split("\n")
        for index, part in enumerate(parts):
            if part:
                lines[-1].append({**run, "text": part})
            if index < len(parts) - 1:
                lines.append([])
    return lines


def _text_image_with_runs(item: dict, box: list[int], text: str, runs: list[dict]) -> tuple[Image.Image, dict]:
    alignment = str(item.get("alignment") or "left").lower()
    if alignment not in {"left", "center", "right"}:
        _fail("TEXT_ALIGNMENT_INVALID", f"Text {item.get('id')} has unsupported alignment {alignment}")
    auto_leading = bool(item.get("auto_leading", True))
    leading_pt = float(item.get("leading_pt") or 0.0)
    if not auto_leading and (not math.isfinite(leading_pt) or leading_pt <= 0):
        _fail("TEXT_LEADING_INVALID", f"Text {item.get('id')} needs a positive leading_pt when auto_leading is false")

    lines = _style_run_lines(runs)
    dummy = Image.new("RGBA", (1, 1))
    measure = ImageDraw.Draw(dummy)
    default_font = runs[0]["font"]
    placements: list[tuple[str, float, float, dict]] = []
    measured_boxes: list[tuple[float, float, float, float]] = []
    baseline = 0.0
    for line_index, segments in enumerate(lines):
        line_ascent = max((run["font"].getmetrics()[0] for run in segments), default=default_font.getmetrics()[0])
        if line_index == 0:
            baseline = float(line_ascent)
        advances = [run["font"].getlength(run["text"]) for run in segments]
        tracking_px = [run["tracking"] * run["font"].size / 1000.0 for run in segments]
        line_width = sum(advance + track * len(run["text"])
                         for advance, track, run in zip(advances, tracking_px, segments))
        if segments:
            line_width -= tracking_px[-1]
        if line_width > box[2] + 1e-6:
            _fail("TEXT_STYLE_DOES_NOT_FIT", f"Declared mixed-size text exceeds layout width for {item.get('id')}")
        x = 0.0 if alignment == "left" else ((box[2] - line_width) / 2.0 if alignment == "center" else box[2] - line_width)
        remaining = sum(len(run["text"]) for run in segments)
        for run, advance, track_px in zip(segments, advances, tracking_px):
            segment_x = x
            if run["tracking"] == 0:
                bounds = measure.textbbox((segment_x, baseline), run["text"], font=run["font"], anchor="ls")
                measured_boxes.append(tuple(float(value) for value in bounds))
                placements.append((run["text"], segment_x, baseline, run))
                x += advance
                remaining -= len(run["text"])
            else:
                for char in run["text"]:
                    bounds = measure.textbbox((x, baseline), char, font=run["font"], anchor="ls")
                    measured_boxes.append(tuple(float(value) for value in bounds))
                    placements.append((char, x, baseline, run))
                    x += run["font"].getlength(char)
                    remaining -= 1
                    if remaining:
                        x += track_px
                # The next style range starts at the same shared baseline.
            if run["tracking"] == 0 and remaining and track_px:
                x += track_px * len(run["text"])
        if line_index < len(lines) - 1:
            baseline += leading_pt if not auto_leading else float(line_ascent)

    if not measured_boxes:
        _fail("TEXT_STYLE_RUN_EMPTY", f"Text {item.get('id')} has no drawable style-run characters")
    top = min(bounds[1] for bounds in measured_boxes)
    bottom = max(bounds[3] for bounds in measured_boxes)
    left = min(bounds[0] for bounds in measured_boxes)
    right = max(bounds[2] for bounds in measured_boxes)
    if bottom - top > box[3] + 1e-6:
        _fail("TEXT_STYLE_DOES_NOT_FIT", f"Declared mixed-size text exceeds layout height for {item.get('id')}")
    if left < -1e-6 or right > box[2] + 1e-6:
        _fail("TEXT_STYLE_DOES_NOT_FIT", f"Declared mixed-size text exceeds layout width for {item.get('id')}")

    image = Image.new("RGBA", (box[2], box[3]), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    y_shift = -top
    for value, x, baseline_y, run in placements:
        draw.text((x, baseline_y + y_shift), value, font=run["font"], fill=run["text_color"], anchor="ls")
    if image.getchannel("A").getbbox() is None:
        _fail("TEXT_STYLE_RUN_EMPTY", f"Text {item.get('id')} mixed-style preview is empty")

    rotation_deg = float(item.get("rotation_deg", 0.0))
    if abs(rotation_deg) > 1e-9:
        image = image.rotate(-rotation_deg, resample=Image.Resampling.BICUBIC, expand=True)
    first = runs[0]
    style_runs = [{key: run[key] for key in ("text", "font_weight", "font_used", "font_file", "font_size_pt",
                                               "text_color", "color_rgba", "tracking")}
                  for run in runs]
    style = {
        "text": text, "font_family": TEXT_FONT_FAMILY, "font_weight": first["font_weight"],
        "font_used": first["font_used"], "font_file": first["font_file"],
        "font_size_pt": first["font_size_pt"], "text_color": first["text_color"],
        "style_runs": style_runs, "alignment": alignment, "tracking": int(item.get("tracking", 0)),
        "auto_leading": auto_leading, "leading_pt": leading_pt, "rotation_deg": rotation_deg,
        **({"rotation_anchor_px": list(item["rotation_anchor_px"])} if item.get("rotation_anchor_px") else {}),
    }
    overrides = {key: item[key] for key in ("tracking", "alignment", "auto_leading", "leading_pt", "layout_box_px",
                                             "rotation_deg", "rotation_anchor_px") if key in item}
    if overrides:
        style["style_overrides"] = overrides
    return image, style


def _text_font(box: list[int], text: str, weight: str, requested_size_pt: float | None,
               *, spacing: int = 0, tracking: int = 0) -> tuple[ImageFont.FreeTypeFont, float, Path]:
    if weight not in FONT_WEIGHT_OPTIONS:
        _fail("TEXT_FONT_WEIGHT_INVALID", f"Unsupported Source Han Sans CN weight: {weight}")
    font_path = FONT_DIR / f"SourceHanSansCN-{weight}.otf"
    if not font_path.is_file():
        _fail("SOURCE_HAN_FONT_MISSING", f"Required local Source Han Sans CN face is missing: {font_path}")
    width, height = box[2], box[3]
    lines = text.splitlines() or [text]
    if requested_size_pt is not None:
        point_size = float(requested_size_pt)
        pixel_size = max(1, int(round(point_size)))
        try:
            font = ImageFont.truetype(str(font_path), pixel_size)
        except OSError as exc:
            _fail("SOURCE_HAN_FONT_UNREADABLE", f"Cannot load {font_path}: {exc}")
        dummy = Image.new("RGBA", (1, 1))
        draw = ImageDraw.Draw(dummy)
        bounds = _text_preview_bbox(draw, text, font, spacing, tracking)
        if bounds[2] - bounds[0] > width or bounds[3] - bounds[1] > height:
            _fail("TEXT_STYLE_DOES_NOT_FIT", f"Declared {point_size:g} pt {weight} text does not fit box {box}")
        return font, point_size, font_path
    size = max(1, min(height, int(height / max(1, len(lines)) * 0.82)))
    try:
        for point_size in range(size, 0, -1):
            font = ImageFont.truetype(str(font_path), point_size)
            dummy = Image.new("RGBA", (1, 1))
            draw = ImageDraw.Draw(dummy)
            bounds = _text_preview_bbox(draw, text, font, spacing, tracking)
            if bounds[2] - bounds[0] <= width and bounds[3] - bounds[1] <= height:
                return font, float(point_size), font_path
        return ImageFont.truetype(str(font_path), 1), 1.0, font_path
    except OSError as exc:
        _fail("SOURCE_HAN_FONT_UNREADABLE", f"Cannot load {font_path}: {exc}")


def _text_image(item: dict) -> tuple[Image.Image, dict]:
    source_box = _positive_box(item.get("source_box_px"), f"text {item.get('id')}")
    has_style_overrides = any(key in item for key in ("layout_box_px", "tracking", "alignment", "auto_leading",
                                                       "leading_pt", "rotation_deg", "rotation_anchor_px", "style_runs"))
    box = _positive_box(item.get("layout_box_px") or source_box, f"text {item.get('id')}.layout_box_px")
    text = item["text"]
    runs = _resolved_style_runs(item, text)
    if runs is not None:
        return _text_image_with_runs(item, box, text, runs)
    text_color = _rgba(item.get("color_rgba"), TEXT_RGBA)
    weight = str(item.get("font_weight") or "Normal")
    alignment = str(item.get("alignment") or "left").lower()
    tracking = int(item.get("tracking", 0))
    auto_leading = bool(item.get("auto_leading", True))
    leading_pt = float(item.get("leading_pt") or 0.0)
    spacing = 0
    if has_style_overrides and not auto_leading:
        size_hint = max(1, int(round(float(item.get("font_size_pt") or 12.0))))
        probe_font = ImageFont.truetype(str(FONT_DIR / f"SourceHanSansCN-{weight}.otf"), size_hint)
        spacing = _text_spacing(probe_font, {"auto_leading": False, "leading_pt": leading_pt})
    font, size, font_path = _text_font(box, text, weight, item.get("font_size_pt"),
                                       spacing=spacing, tracking=tracking if has_style_overrides else 0)
    image = Image.new("RGBA", (box[2], box[3]), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if not has_style_overrides:
        bounds = draw.multiline_textbbox((0, 0), text, font=font, spacing=0)
        draw.multiline_text((-bounds[0], -bounds[1]), text, font=font, fill=text_color, spacing=0)
    else:
        bounds = _text_preview_bbox(draw, text, font, spacing, tracking)
        if tracking == 0 and alignment == "left":
            draw.multiline_text((-bounds[0], -bounds[1]), text, font=font, fill=text_color, spacing=spacing)
        else:
            lines = text.splitlines() or [text]
            tracking_px = tracking * font.size / 1000.0
            line_step = font.getmetrics()[0] + spacing
            for index, line in enumerate(lines):
                line_width = sum(font.getlength(char) for char in line) + max(0, len(line) - 1) * tracking_px
                x = 0.0 if alignment == "left" else ((box[2] - line_width) / 2.0 if alignment == "center" else box[2] - line_width)
                y = -bounds[1] + index * line_step
                for char in line:
                    draw.text((int(round(x)), y), char, font=font, fill=text_color)
                    x += font.getlength(char) + tracking_px
    rotation_deg = float(item.get("rotation_deg", 0.0))
    if abs(rotation_deg) > 1e-9:
        image = image.rotate(-rotation_deg, resample=Image.Resampling.BICUBIC, expand=True)
    style = {
        "text": text,
        "font_family": TEXT_FONT_FAMILY,
        "font_weight": weight,
        "font_used": f"{TEXT_FONT_FAMILY}-{weight}",
        "font_file": font_path.name,
        "font_size_pt": float(size),
        "text_color": text_color,
        "alignment": alignment,
        "tracking": tracking,
        "auto_leading": auto_leading,
        "leading_pt": leading_pt,
        "rotation_deg": rotation_deg,
        **({"rotation_anchor_px": list(item["rotation_anchor_px"])} if item.get("rotation_anchor_px") else {}),
    }
    overrides = {key: item[key] for key in ("tracking", "alignment", "auto_leading", "leading_pt", "layout_box_px", "rotation_deg", "rotation_anchor_px") if key in item}
    if overrides:
        style["style_overrides"] = overrides
    return image, style


def _read_stage2(page_path: Path) -> tuple[dict, Path, Path, tuple[int, int]]:
    root = page_path.parent.resolve()
    try:
        page = json.loads(page_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise Stage3Error(f"Cannot read Stage 2 page_rebuild.json: {exc}") from exc
    if page.get("schema_version") != 1:
        _fail("STAGE2_SCHEMA_UNSUPPORTED", "Only page_rebuild.json schema_version 1 is supported")
    background = page.get("background") if isinstance(page.get("background"), dict) else {}
    if page.get("status") == "pending_clean_base" or background.get("status") != "ready":
        _fail("PENDING_CLEAN_BASE", "Stage 3 refuses PSD writing while Stage 2 has no ready clean background")
    if page.get("status") != "ready_for_stage_3":
        _fail("STAGE2_NOT_READY", "Stage 2 status must be ready_for_stage_3")
    if background.get("decision") != "imported_clean_base":
        _fail("EXPLICIT_CLEAN_BASE_REQUIRED", "Stage 3 requires an imported clean base; source.png cannot be a fallback")
    source_info = page.get("source") if isinstance(page.get("source"), dict) else {}
    size_info = source_info.get("size_px") if isinstance(source_info.get("size_px"), dict) else {}
    width, height = size_info.get("width"), size_info.get("height")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (width, height)):
        _fail("SOURCE_SIZE_INVALID", "Stage 2 source.size_px must contain positive pixel dimensions")
    source_rel = source_info.get("source_png_path")
    if not isinstance(source_rel, str) or source_rel != "../source.png":
        _fail("SOURCE_PATH_INVALID", "Stage 3 expects the Stage 2 run-local ../source.png identity")
    source_path = (root / source_rel).resolve()
    if source_path != (root.parent / "source.png").resolve() or not source_path.is_file():
        _fail("SOURCE_PATH_INVALID", "Stage 2 source.png does not resolve beside its run directory")
    with Image.open(source_path) as source_image:
        if source_image.size != (width, height):
            _fail("SOURCE_SIZE_MISMATCH", "Stage 2 canvas dimensions differ from source.png")
    source_hash = source_info.get("source_png_sha256")
    if not isinstance(source_hash, str) or sha256_file(source_path) != source_hash:
        _fail("SOURCE_HASH_MISMATCH", "Stage 2 source.png hash does not match page_rebuild.json")
    background_path = _contained_path(root, background.get("path"), "clean background")
    background_hash = sha256_file(background_path)
    if background_path == source_path or background_hash == source_hash or background_hash != background.get("sha256"):
        _fail("CLEAN_BASE_IDENTITY_INVALID", "Clean background must be a hash-matched file distinct from source.png")
    if _rgba_pixel_hash(background_path) == _rgba_pixel_hash(source_path):
        _fail("CLEAN_BASE_IDENTITY_INVALID", "Clean background pixels must differ from source.png")
    with Image.open(background_path) as image:
        if image.size != (width, height):
            _fail("CLEAN_BASE_SIZE_MISMATCH", "Clean background must match source pixel dimensions")
    return page, root, source_path, (width, height)


def _fit_asset(asset: Image.Image, source_box: list[int]) -> tuple[Image.Image, int, int, float, list[int], list[int]]:
    x, y, width, height = source_box
    content = asset.getchannel("A").getbbox()
    if content is None:
        _fail("ASSET_ALPHA_EMPTY", "Asset has no visible alpha content")
    content_width, content_height = content[2] - content[0], content[3] - content[1]
    scale = min(width / content_width, height / content_height)
    for _ in range(12):
        fitted_width = max(1, int(round(asset.width * scale)))
        fitted_height = max(1, int(round(asset.height * scale)))
        fitted = asset.resize((fitted_width, fitted_height), Image.Resampling.LANCZOS)
        resized_content = fitted.getchannel("A").getbbox()
        if resized_content is None:
            _fail("ASSET_ALPHA_EMPTY", "Resizing removed all visible alpha content")
        visible_width = resized_content[2] - resized_content[0]
        visible_height = resized_content[3] - resized_content[1]
        if visible_width <= width and visible_height <= height:
            break
        scale *= min(width / visible_width, height / visible_height) * 0.995
    else:
        _fail("ASSET_CONTAIN_FAILED", "Visible alpha content did not fit its source_box_px after uniform scaling")
    left = x + (width - visible_width) // 2 - resized_content[0]
    top = y + (height - visible_height) // 2 - resized_content[1]
    layer_box = [left, top, fitted_width, fitted_height]
    placed_content_box = [left + resized_content[0], top + resized_content[1], visible_width, visible_height]
    return fitted, left, top, scale, layer_box, placed_content_box


def _build_layers(page: dict, root: Path, canvas: tuple[int, int]) -> tuple[list[dict], dict[str, dict]]:
    width, height = canvas
    background_spec = page["background"]
    background_path = _contained_path(root, background_spec["path"], "clean background")
    with Image.open(background_path) as source:
        background = source.convert("RGBA")
        background.load()
    layers_back_to_front = [{
        "id": "clean-background", "name": "Background - clean base", "role": "background",
        "source_box_px": [0, 0, width, height], "image": background,
        "left": 0, "top": 0,
        "meta": {"id": "clean-background", "role": "background", "source_box_px": [0, 0, width, height], "asset_sha256": background_spec["sha256"]},
    }]
    assets = page.get("foreground_assets", [])
    texts = page.get("text_inventory", [])
    if not isinstance(assets, list) or not isinstance(texts, list):
        _fail("STAGE2_ITEMS_INVALID", "foreground_assets and text_inventory must be lists")
    asset_contracts = {item.get("visual_unit_id"): item for item in assets if isinstance(item, dict)}
    for text in texts:
        if not isinstance(text, dict) or not text.get("visual_unit_id"):
            continue
        owner_id = text["visual_unit_id"]
        owner = asset_contracts.get(owner_id)
        owner_input = owner.get("asset_input") if isinstance(owner, dict) and isinstance(owner.get("asset_input"), dict) else {}
        if (owner is None or owner.get("native_text_extraction_allowed") is not True
                or owner.get("internal_text_retained") is not False or owner_input.get("text_free_base") is not True
                or owner_input.get("kind") == "source_visual_unit"):
            _fail("TEXT_VISUAL_UNIT_INVALID", f"Text {text.get('id')} requires an existing text_free_base visual unit")
        tx, ty, tw, th = _positive_box(text.get("source_box_px"), f"text {text.get('id')}")
        ox, oy, ow, oh = _positive_box(owner.get("source_box_px"), f"asset {owner_id}")
        if tx < ox or ty < oy or tx + tw > ox + ow or ty + th > oy + oh:
            _fail("TEXT_VISUAL_UNIT_BOUNDS_INVALID", f"Text {text.get('id')} exceeds visual unit {owner_id}")
    seen = {"clean-background"}
    for item in assets:
        if not isinstance(item, dict) or not isinstance(item.get("visual_unit_id"), str):
            _fail("ASSET_CONTRACT_INVALID", "Every foreground asset needs a visual_unit_id")
        asset_id = item["visual_unit_id"]
        if asset_id in seen:
            _fail("DUPLICATE_LAYER_ID", f"Duplicate layer id: {asset_id}")
        seen.add(asset_id)
        box = _positive_box(item.get("source_box_px"), f"asset {asset_id}")
        if box[0] + box[2] > width or box[1] + box[3] > height:
            _fail("SOURCE_BOX_INVALID", f"Asset {asset_id} exceeds the source canvas")
        path = _contained_path(root, item.get("asset_path"), f"asset {asset_id}")
        digest = sha256_file(path)
        if digest != item.get("asset_sha256") or digest == page["source"].get("source_png_sha256"):
            _fail("ASSET_IDENTITY_INVALID", f"Asset {asset_id} hash is invalid or uses source.png")
        with Image.open(path) as source:
            asset = source.convert("RGBA")
            asset.load()
        if asset.getchannel("A").getbbox() is None:
            _fail("ASSET_ALPHA_EMPTY", f"Asset {asset_id} has no visible pixels")
        asset_input = item.get("asset_input") if isinstance(item.get("asset_input"), dict) else {}
        is_source_visual_unit = asset_input.get("kind") == "source_visual_unit"
        is_text_free_base = item.get("native_text_extraction_allowed") is True and asset_input.get("text_free_base") is True
        allow_opaque = is_source_visual_unit or is_text_free_base
        text_owner = item["visual_unit_id"] if item.get("native_text_extraction_allowed") else None
        fitted, left, top, scale, layer_box, placed_content_box = _fit_asset(asset, box)
        spec = {
            "id": f"asset-{asset_id}", "name": f"Asset - {asset_id}", "role": "asset",
            "source_box_px": box, "image": fitted, "left": left, "top": top,
            "allow_opaque": allow_opaque,
            "visual_unit_id": text_owner,
            "meta": {"id": f"asset-{asset_id}", "role": "asset", "source_box_px": box,
                     "asset_sha256": digest, "asset_size_px": list(asset.size),
                     "source_visual_unit": is_source_visual_unit,
                     "text_free_base": is_text_free_base,
                     "asset_content_box_px": list(asset.getchannel("A").getbbox()),
                     "layer_box_px": layer_box, "placed_content_box_px": placed_content_box,
                     "uniform_scale": scale, "scale_xy": [scale, scale],
                     "placement": "contain", "internal_text_retained": bool(item.get("internal_text_retained"))},
        }
        layers_back_to_front.append(spec)
    text_specs = {}
    for item in texts:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            _fail("TEXT_CONTRACT_INVALID", "Every main text item needs an id")
        text_id = item["id"]
        if text_id in seen:
            _fail("DUPLICATE_LAYER_ID", f"Duplicate layer id: {text_id}")
        seen.add(text_id)
        if not isinstance(item.get("text"), str) or not item["text"] or "\x00" in item["text"]:
            _fail("TEXT_CONTENT_INVALID", f"Text {text_id} must contain exact non-empty text")
        source_box = _positive_box(item.get("source_box_px"), f"text {text_id}")
        if source_box[0] + source_box[2] > width or source_box[1] + source_box[3] > height:
            _fail("SOURCE_BOX_INVALID", f"Text {text_id} exceeds the source canvas")
        layout_box = _positive_box(item.get("layout_box_px") or source_box, f"text {text_id}.layout_box_px")
        if layout_box[0] + layout_box[2] > width or layout_box[1] + layout_box[3] > height:
            _fail("LAYOUT_BOX_INVALID", f"Text {text_id} layout box exceeds the source canvas")
        image, type_style = _text_image(item)
        rotation_deg = float(type_style["rotation_deg"])
        target_center = _rotated_center(layout_box, rotation_deg, item.get("rotation_anchor_px"))
        frame_center = (layout_box[0] + layout_box[2] / 2.0, layout_box[1] + layout_box[3] / 2.0)
        dx, dy = target_center[0] - frame_center[0], target_center[1] - frame_center[1]
        left = int(round(layout_box[0] + (layout_box[2] - image.width) / 2.0 + dx))
        top = int(round(layout_box[1] + (layout_box[3] - image.height) / 2.0 + dy))
        if abs(type_style["rotation_deg"]) > 1e-9:
            alpha_box = image.getchannel("A").getbbox()
            if alpha_box is None:
                _fail("ROTATED_TEXT_ALPHA_EMPTY", f"Rotated text {text_id} has no visible pixels")
            ink_bounds = (left + alpha_box[0], top + alpha_box[1], left + alpha_box[2], top + alpha_box[3])
            if (ink_bounds[0] < 0 or ink_bounds[1] < 0
                    or ink_bounds[2] > width or ink_bounds[3] > height):
                _fail("ROTATED_TEXT_OUTSIDE_CANVAS", f"Rotated text {text_id} ink {ink_bounds} exceeds canvas {(width, height)}")
        owner_id = item.get("visual_unit_id")
        spec = {"id": f"text-{text_id}", "name": f"Text - {text_id}", "role": "text", "source_box_px": source_box,
                "image": image, "left": left, "top": top, "text": item["text"], "type_style": type_style,
                "rotation": type_style["rotation_deg"],
                "visual_unit_id": owner_id,
                "meta": {"id": f"text-{text_id}", "role": "text", "text": item["text"],
                         "source_box_px": source_box,
                         **({"visual_unit_id": owner_id, "group_local_box_px": item.get("group_local_box_px")} if owner_id else {}),
                         "font_family": type_style["font_family"],
                         "font_weight": type_style["font_weight"],
                         "font_used": type_style["font_used"],
                         "font_file": type_style["font_file"],
                         "font_size_pt": type_style["font_size_pt"],
                         **({"style_runs": type_style["style_runs"]} if type_style.get("style_runs") else {}),
                         "rotation_deg": type_style["rotation_deg"],
                         **({"rotation_anchor_px": list(item["rotation_anchor_px"])}
                            if item.get("rotation_anchor_px") else {})}}
        if item.get("layout_box_px") is not None:
            spec["layout_box_px"] = layout_box
            spec["meta"]["layout_box_px"] = layout_box
        layers_back_to_front.append(spec)
        text_specs[spec["name"]] = spec
    return layers_back_to_front, text_specs


def _write_psd(path: Path, layers_back_to_front: list[dict], text_specs: dict[str, dict], canvas: tuple[int, int]) -> None:
    try:
        from pytoshop import enums, tagged_block
        from pytoshop.user.nested_layers import nested_layers_to_psd
    except ImportError as exc:
        raise Stage3Error("Install the Stage 3 dependencies from requirements.txt") from exc
    width, height = canvas
    children = []
    by_owner: dict[str, list[dict]] = {}
    for layer in layers_back_to_front:
        if layer.get("visual_unit_id"):
            by_owner.setdefault(layer["visual_unit_id"], []).append(layer)
    emitted = set()
    owner_group_numbers = {}

    def make_pixel_layer(layer: dict, group_number: int):
        return _pimage(
            {"id": layer["id"], "name": layer["name"], "visible": True, "opacity": 255, "blend_mode": "normal"},
            layer["image"], left=layer["left"], top=layer["top"], group_number=group_number, meta=layer["meta"],
        )

    for layer in reversed(layers_back_to_front):
        owner_id = layer.get("visual_unit_id")
        if owner_id:
            if layer["role"] != "asset" or owner_id in emitted:
                continue
            emitted.add(owner_id)
            unit_group_number = 2 + len(owner_group_numbers)
            owner_group_numbers[owner_id] = unit_group_number
            unit_children = [make_pixel_layer(child, unit_group_number) for child in reversed(by_owner[owner_id])]
            children.append(_pgroup(
                {"id": f"visual-unit-{owner_id}", "name": f"Visual Unit - {owner_id}", "visible": True,
                 "opacity": 255, "blend_mode": "pass-through"},
                unit_children, 1, {"id": f"visual-unit-{owner_id}", "role": "visual-unit", "visual_unit_id": owner_id},
            ))
        else:
            children.append(make_pixel_layer(layer, 1))
    group = _pgroup({"id": "poster", "name": "Poster", "visible": True, "opacity": 255, "blend_mode": "pass-through"}, children, 1,
                    {"id": "poster", "role": "poster-container"})
    psd = nested_layers_to_psd([group], enums.ColorMode.rgb, depth=enums.ColorDepth.depth8,
                               compression=enums.Compression.raw, size=(width, height))
    text_by_name = {name: spec for name, spec in text_specs.items()}
    writer_records = psd.layer_and_mask_info.layer_info.layer_records
    seen_text = set()
    for record in writer_records:
        if not getattr(record, "channels", None):
            continue
        name = str(getattr(record, "name", "")).rstrip("\x00")
        if name in text_by_name:
            spec = text_by_name[name]
            record.blocks.append(tagged_block.GenericTaggedBlock(
                code=b"TySh", data=_text_block(spec, spec["type_style"]).tobytes(padding=4),
            ))
            seen_text.add(name)
    if seen_text != set(text_by_name):
        _fail("NATIVE_TEXT_WRITE_FAILED", "One or more native text layers did not receive a TySh block")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        psd.write(output)


def _reopen_validate(path: Path, layers_back_to_front: list[dict], canvas: tuple[int, int]) -> tuple[Image.Image, dict]:
    try:
        from psd_tools import PSDImage
    except ImportError as exc:
        raise Stage3Error("Install psd-tools from requirements.txt") from exc
    try:
        psd = PSDImage.open(path)
    except Exception as exc:
        raise Stage3Error(f"PSD could not be reopened: {exc}") from exc
    if tuple(psd.size) != canvas or psd.depth != 8:
        _fail("PSD_REOPEN_CANVAS_MISMATCH", "Reopened PSD canvas or bit depth differs from source.png")
    if len(psd) != 1 or getattr(psd[0], "kind", None) != "group":
        _fail("PSD_REOPEN_GROUP_MISMATCH", "Expected one Poster group")
    leaves = [layer for layer in psd.descendants() if getattr(layer, "kind", None) != "group"]
    groups = [layer for layer in psd.descendants() if getattr(layer, "kind", None) == "group"]
    expected_order = []
    emitted_units = set()
    by_owner = {}
    for item in layers_back_to_front:
        if item.get("visual_unit_id"):
            by_owner.setdefault(item["visual_unit_id"], []).append(item)
    for item in layers_back_to_front:
        owner_id = item.get("visual_unit_id")
        if not owner_id:
            expected_order.append(item["name"])
        elif item["role"] == "asset" and owner_id not in emitted_units:
            emitted_units.add(owner_id)
            expected_order.extend(child["name"] for child in by_owner[owner_id])
    actual_order = [str(layer.name).rstrip("\x00") for layer in leaves]
    if actual_order != expected_order:
        _fail("PSD_REOPEN_LAYER_ORDER_MISMATCH", f"Expected back-to-front {expected_order}, got {actual_order}")
    by_name = {str(layer.name).rstrip("\x00"): layer for layer in leaves}
    if len(by_name) != len(expected_order):
        _fail("PSD_REOPEN_LAYER_COUNT_MISMATCH", "Reopened PSD has duplicate or missing leaf layers")
    evidence = []
    for spec in layers_back_to_front:
        layer = by_name[spec["name"]]
        owner_id = spec.get("visual_unit_id")
        if owner_id:
            expected_group = f"Visual Unit - {owner_id}"
            parents = [group for group in groups if str(group.name).rstrip("\x00") == expected_group]
            parent_name = str(getattr(getattr(layer, "parent", None), "name", "")).rstrip("\x00")
            if len(parents) != 1 or parent_name != expected_group:
                _fail("PSD_REOPEN_TEXT_GROUP_MISMATCH", f"{spec['name']} is not inside {expected_group}")
            if spec["role"] == "text":
                evidence_item_owner = {"visual_unit_id": owner_id,
                                       "group_local_box_px": spec["meta"].get("group_local_box_px")}
            else:
                evidence_item_owner = {"visual_unit_id": owner_id}
        else:
            evidence_item_owner = {}
        bbox = tuple(int(value) for value in layer.bbox)
        expected_bbox = (spec["left"], spec["top"], spec["left"] + spec["image"].width, spec["top"] + spec["image"].height)
        if bbox != expected_bbox:
            _fail("PSD_REOPEN_LAYER_BOUNDS_MISMATCH", f"{spec['name']} bounds {bbox} differ from {expected_bbox}")
        evidence_item = {"name": spec["name"], "role": spec["role"], "bbox": list(bbox), "source_box_px": spec["source_box_px"]}
        evidence_item.update(evidence_item_owner)
        if spec["role"] == "asset":
            pixels = layer.topil()
            if pixels is None or pixels.mode not in {"RGBA", "LA"}:
                _fail("PSD_REOPEN_ASSET_NOT_ALPHA", f"{spec['name']} did not reopen as a transparent pixel layer")
            alpha = pixels.convert("RGBA").getchannel("A")
            is_opaque = alpha.getextrema() == (255, 255)
            if alpha.getbbox() is None or (is_opaque and not spec.get("allow_opaque")):
                _fail("PSD_REOPEN_ASSET_ALPHA_MISSING", f"{spec['name']} lost its transparency")
            evidence_item["alpha_extrema"] = list(alpha.getextrema())
            evidence_item["opaque_source_visual_unit"] = bool(is_opaque and spec["meta"].get("source_visual_unit"))
            evidence_item["opaque_text_free_base"] = bool(is_opaque and spec["meta"].get("text_free_base"))
            alpha_box = alpha.getbbox()
            visible_box = [bbox[0] + alpha_box[0], bbox[1] + alpha_box[1], alpha_box[2] - alpha_box[0], alpha_box[3] - alpha_box[1]]
            if visible_box != spec["meta"].get("placed_content_box_px"):
                _fail("PSD_REOPEN_ASSET_PLACEMENT_MISMATCH", f"{spec['name']} visible pixels moved outside its contain placement")
            evidence_item.update({
                "asset_content_box_px": spec["meta"]["asset_content_box_px"],
                "layer_box_px": spec["meta"]["layer_box_px"],
                "placed_content_box_px": visible_box,
                "uniform_scale": spec["meta"]["uniform_scale"],
                "scale_xy": spec["meta"]["scale_xy"],
            })
        elif spec["role"] == "text":
            parsed = str(getattr(layer, "text", ""))
            normalize = lambda value: value.replace("\r\n", "\n").replace("\r", "\n")
            if layer.kind != "type" or normalize(parsed) != normalize(spec["text"]):
                _fail("PSD_REOPEN_TEXT_NOT_NATIVE", f"{spec['name']} did not reopen with exact native TySh text")
            style_run_entries = list(layer.engine_dict["StyleRun"]["RunArray"])
            style_run_lengths = [int(value) for value in layer.engine_dict["StyleRun"]["RunLengthArray"]]
            actual_styles = [entry["StyleSheet"]["StyleSheetData"] for entry in style_run_entries]
            style_data = actual_styles[0]
            font_size = float(style_data["FontSize"])
            tracking = int(style_data["Tracking"])
            auto_leading = bool(style_data["AutoLeading"])
            leading_pt = float(style_data["Leading"])
            paragraph_data = layer.engine_dict["ParagraphRun"]["RunArray"][0]["ParagraphSheet"]["Properties"]
            justification = int(paragraph_data["Justification"])
            font_names = list(layer.font_names)
            expected_style = spec["type_style"]
            expected_style_runs = expected_style.get("style_runs")
            if (expected_style["font_used"] not in font_names
                    or abs(font_size - expected_style["font_size_pt"]) > 0.01
                    or tracking != expected_style.get("tracking", 0)
                    or auto_leading != expected_style.get("auto_leading", True)
                    or abs(leading_pt - expected_style.get("leading_pt", 0.0)) > 0.01
                    or justification != {"left": 0, "right": 1, "center": 2}.get(expected_style.get("alignment", "left"), 0)):
                _fail("PSD_REOPEN_TEXT_STYLE_MISMATCH", f"{spec['name']} lost its Source Han Sans CN weight or size")
            if expected_style_runs:
                expected_lengths = [_utf16_units(run["text"].replace("\r\n", "\r").replace("\n", "\r"))
                                    for run in expected_style_runs]
                expected_lengths[-1] += _utf16_units("\r")
                if len(actual_styles) != len(expected_style_runs) or style_run_lengths != expected_lengths:
                    _fail("PSD_REOPEN_STYLE_RUN_LENGTH_MISMATCH", f"{spec['name']} lost or duplicated style-run text units")
                font_set = list(layer.resource_dict["FontSet"])
                reopened_style_runs = []
                for index, (actual, expected_run) in enumerate(zip(actual_styles, expected_style_runs)):
                    font_index = int(actual["Font"])
                    if font_index < 0 or font_index >= len(font_set):
                        _fail("PSD_REOPEN_STYLE_RUN_FONT_MISMATCH", f"{spec['name']} style run {index} has an invalid font index")
                    actual_font_name = str(font_set[font_index]["Name"].value)
                    expected_color = expected_run["text_color"]
                    fill_values = [float(value) for value in actual["FillColor"]["Values"]]
                    normalized_color = [expected_color[3] / 255.0, expected_color[0] / 255.0,
                                        expected_color[1] / 255.0, expected_color[2] / 255.0]
                    if (actual_font_name != expected_run["font_used"]
                            or abs(float(actual["FontSize"]) - expected_run["font_size_pt"]) > 0.01
                            or int(actual["Tracking"]) != expected_run["tracking"]
                            or bool(actual["AutoLeading"]) != expected_style.get("auto_leading", True)
                            or abs(float(actual["Leading"]) - expected_style.get("leading_pt", 0.0)) > 0.01
                            or abs(float(actual["BaselineShift"])) > 0.001
                            or len(fill_values) != 4
                            or any(abs(actual_value - expected_value) > 0.005
                                   for actual_value, expected_value in zip(fill_values, normalized_color))):
                        _fail("PSD_REOPEN_STYLE_RUN_MISMATCH",
                              f"{spec['name']} style run {index} actual font/size/tracking/color/leading/baseline "
                              f"{actual_font_name!r}/{float(actual['FontSize']):g}/{int(actual['Tracking'])}/"
                              f"{fill_values}/{bool(actual['AutoLeading'])}/{float(actual['Leading']):g}/"
                              f"{float(actual['BaselineShift']):g}; expected "
                              f"{expected_run['font_used']!r}/{expected_run['font_size_pt']:g}/"
                              f"{expected_run['tracking']}/{normalized_color}/"
                              f"{expected_style.get('auto_leading', True)}/{expected_style.get('leading_pt', 0.0):g}/0")
                    reopened_style_runs.append({
                        "text": expected_run["text"], "font_used": actual_font_name,
                        "font_size_pt": float(actual["FontSize"]), "color_rgba": list(expected_color),
                        "tracking": int(actual["Tracking"]), "run_length_utf16": style_run_lengths[index],
                        "auto_leading": bool(actual["AutoLeading"]), "leading_pt": float(actual["Leading"]),
                        "baseline_shift_pt": float(actual["BaselineShift"]),
                    })
            else:
                expected_editor = spec["text"].replace("\r\n", "\r").replace("\n", "\r") + "\r"
                if len(actual_styles) != 1 or style_run_lengths != [_utf16_units(expected_editor)]:
                    _fail("PSD_REOPEN_STYLE_RUN_LENGTH_MISMATCH", f"{spec['name']} changed its uniform style run length")
                reopened_style_runs = []
            evidence_item.update({"kind": layer.kind, "text": spec["text"],
                                  "color_rgba": list(spec["type_style"]["text_color"]),
                                  "font_family": expected_style["font_family"],
                                  "font_weight": expected_style["font_weight"],
                                  "font_used": expected_style["font_used"],
                                  "font_size_pt": font_size})
            if expected_style_runs:
                evidence_item["style_runs"] = reopened_style_runs
            if "layout_box_px" in spec:
                evidence_item["layout_box_px"] = spec["layout_box_px"]
            if expected_style.get("style_overrides"):
                evidence_item.update({"tracking": tracking, "auto_leading": auto_leading,
                                      "leading_pt": leading_pt, "alignment": expected_style["alignment"],
                                      "justification": justification})
            if abs(expected_style.get("rotation_deg", 0.0)) > 1e-9:
                tysh = layer._record.tagged_blocks.get_data(b"TySh")
                if tysh is None:
                    _fail("PSD_REOPEN_TEXT_TRANSFORM_MISSING", f"{spec['name']} lost its native text transform")
                actual_transform = tuple(float(value) for value in tysh.transform)
                box = _positive_box(spec.get("layout_box_px") or spec["source_box_px"], spec["name"])
                expected_transform = _text_transform(
                    box, expected_style["rotation_deg"], expected_style.get("rotation_anchor_px"))
                if len(actual_transform) != 6 or any(abs(a - b) > 1e-4 for a, b in zip(actual_transform, expected_transform)):
                    _fail("PSD_REOPEN_TEXT_TRANSFORM_MISMATCH", f"{spec['name']} native text transform differs from preview rotation")
                evidence_item["rotation_deg"] = expected_style["rotation_deg"]
                evidence_item["text_transform"] = list(actual_transform)
        evidence.append(evidence_item)
    background = by_name["Background - clean base"]
    if expected_order[0] != "Background - clean base" or background.bbox != (0, 0, canvas[0], canvas[1]):
        _fail("PSD_REOPEN_BACKGROUND_ORDER_INVALID", "Clean background is not the full-canvas bottom layer")
    # psd-tools returns a blank composite for this single root group; the
    # reopened group's composite is the actual page content.
    composite = psd[0].composite()
    if composite is None:
        _fail("PSD_PREVIEW_FAILED", "psd-tools could not composite the reopened PSD")
    preview = composite.convert("RGB")
    if preview.size != canvas:
        preview = preview.crop((0, 0, canvas[0], canvas[1]))
    if preview.size != canvas:
        _fail("PSD_PREVIEW_SIZE_MISMATCH", "Reopened group composite does not cover the PSD canvas")
    return preview, {"canvas_px": {"width": canvas[0], "height": canvas[1]}, "group_count": len(groups),
                     "layer_count": len(leaves), "back_to_front": actual_order, "layers": evidence,
                     "reopened_with": "psd-tools", "photoshop_manual_check": "PENDING_MANUAL_PHOTOSHOP"}


def build_psd(page_rebuild_json: str | Path, output_dir: str | Path | None = None) -> dict:
    page_path = Path(page_rebuild_json).expanduser().resolve()
    page, stage2_root, source_path, canvas = _read_stage2(page_path)
    output_root = Path(output_dir).expanduser().resolve() if output_dir else stage2_root / "stage3"
    output_root.mkdir(parents=True, exist_ok=True)
    layers, text_specs = _build_layers(page, stage2_root, canvas)
    psd_path = output_root / "poster.psd"
    _write_psd(psd_path, layers, text_specs, canvas)
    preview, reopen = _reopen_validate(psd_path, layers, canvas)
    preview_path = output_root / "preview.png"
    preview.save(preview_path, format="PNG")
    manifest = {
        "schema_version": 1, "source_png_sha256": page["source"]["source_png_sha256"],
        "clean_base_sha256": page["background"]["sha256"], "canvas_px": {"width": canvas[0], "height": canvas[1]},
        "psd": psd_path.name, "preview": preview_path.name, "layers": reopen["layers"],
        "reopen_validation": reopen, "writer": "pytoshop", "native_text_type": "TySh",
        "native_text_font_family": TEXT_FONT_FAMILY,
        "native_text_font_source": "local SourceHanSansCN-<Weight>.otf; missing faces are errors",
        "scope": "offline PSD structure and psd-tools reopen check; Photoshop manual open/edit/save/reopen remains pending",
    }
    if isinstance(page.get("diagnostic_label"), str):
        manifest["diagnostic_label"] = page["diagnostic_label"]
    (output_root / "stage3_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a minimal editable PSD from Stage 2 page_rebuild.json.")
    parser.add_argument("page_rebuild_json")
    parser.add_argument("--out-dir")
    args = parser.parse_args()
    try:
        report = build_psd(args.page_rebuild_json, args.out_dir)
    except Exception as exc:
        parser.error(str(exc))
    artifact_root = Path(args.out_dir).expanduser().resolve() if args.out_dir else Path(args.page_rebuild_json).expanduser().resolve().parent / "stage3"
    print(artifact_root / report["psd"])
    print(artifact_root / report["preview"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
