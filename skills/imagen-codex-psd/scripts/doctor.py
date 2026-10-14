#!/usr/bin/env python3
"""Check local runtime and font prerequisites without installing or calling services."""
from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = Path.home() / "Library" / "Fonts"
FONTS = tuple(f"SourceHanSansCN-{weight}.otf" for weight in
              ("ExtraLight", "Light", "Normal", "Regular", "Medium", "Bold", "Heavy"))
MODULES = {"Pillow": "PIL", "numpy": "numpy", "requests": "requests",
           "psd-tools": "psd_tools", "pytoshop": "pytoshop", "PyYAML": "yaml", "six": "six"}


def main() -> int:
    errors = []
    print(f"python={sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    if sys.version_info < (3, 10):
        errors.append("Python 3.10 or newer is required.")
    for distribution, module_name in MODULES.items():
        available = importlib.util.find_spec(module_name) is not None
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            version = "not-installed"
        print(f"dependency:{distribution}={version}; importable={'yes' if available else 'no'}")
        if not available:
            errors.append(f"Missing runtime dependency: {distribution}.")
    available_fonts = [name for name in FONTS if (FONT_DIR / name).is_file()]
    print(f"source_han_fonts={len(available_fonts)}/7; directory={FONT_DIR}")
    if not available_fonts:
        errors.append("No SourceHanSansCN font face found; native Chinese PSD text cannot be built.")
    for path in (ROOT / "scripts/text_first.py", ROOT / "scripts/text_background.py",
                 ROOT / "scripts/prepare_stage3.py", ROOT / "scripts/stage3_writer_adapter.py",
                 ROOT / "scripts/psd_writer.py", ROOT / "scripts/remove_chroma_key.py",
                 ROOT / "scripts/split_alpha_components.py"):
        if not path.is_file():
            errors.append(f"Missing bundled file: {path.relative_to(ROOT)}")
    from ocr_credentials import resolve_credential
    credential = resolve_credential()
    print(f"paddleocr_credential={'configured' if credential.value else 'not-configured'}; source={credential.source}; live_service_validation=not_tested")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("local_runtime=ready; no package install or external service call was performed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
