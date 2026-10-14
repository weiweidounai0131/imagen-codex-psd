# Third-party notices and provenance

## Bundled code

- `scripts/psd_writer.py` contains the small TySh/pytoshop PSD-writing core adapted from the local `imagen-codex-psd` builder. It is distributed under the included MIT license, copyright 2026 ningzimu. The prior four-class workflow/orchestrator is not included.
- `scripts/remove_chroma_key.py` and `scripts/split_alpha_components.py` are sourced from `ningzimu/image-to-editable-ppt-skill` and are distributed under that repository's MIT license. The installed Skill directory had no license file, so the public repository license is retained here: <https://github.com/ningzimu/image-to-editable-ppt-skill/blob/main/LICENSE>.
- The single-image PaddleOCR adapter preserves the previously verified jobs endpoint and output contract. Its result parsing and credential resolution are bundled locally; it does not import the installed PPT Skill runtime.

## Runtime dependencies

`requirements.txt` lists third-party packages that are installed separately into the user's chosen environment; their own licenses apply. Their source code is not vendored. Source Han Sans CN fonts are also not included. Install those fonts from Adobe's Source Han Sans distribution under its SIL Open Font License 1.1: <https://github.com/adobe-fonts/source-han-sans/blob/master/LICENSE.txt>.

## External OCR service

Cloud OCR is provided by PaddleOCR/AI Studio under its current service terms, account limits and quota. The Skill makes no price, quota, or availability promise. The current access-token page and service documentation are linked in `references/ocr-setup.md`.
