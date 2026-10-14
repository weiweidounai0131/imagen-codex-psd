# Single-poster workflow

All relative paths belong to the current run. Never reuse OCR, text plans, roles, or pixel boxes from another source image.

## 1. Prepare and choose scope

Run `text_first.py prepare <image> --out-root <runs>`, then check dependencies and OCR credential status. Tell the user the result will contain a complete background, editable main copy, and separate movable assets only for objects that genuinely need separate restoration. For a new run, use the current session's `functions.request_user_input_async` choice tool once for A “拆出全部主文案” or B “只拆指定文案”. When B is chosen, say that you will first review and number the text, and that the user can choose numbers after seeing the numbered image. Do not ask them to transcribe text or guess numbers before that image exists. Wait for the user's actual answer before recording it with:

```sh
python scripts/selection_gate.py record-mode --run-dir <run> --answer full
# or
python scripts/selection_gate.py record-mode --run-dir <run> --answer partial
```

If the choice tool is pending/queued, times out, or returns no answer, keep `pending_mode`; do not run OCR. Fall back to ordinary conversation only if the target session truly lacks the choice tool, and still wait for an actual answer. Historical runs keep the rules and selection already recorded for them.

## 2. Run or recover OCR

After the scope answer, disclose before the first submission that PaddleOCR sends the image to a third-party service. If a credential is missing, point to the existing application and secure Keychain entry instructions in [ocr-setup.md](ocr-setup.md); never ask the user to paste a token into chat. Offer the existing manual/local route only if the user prefers it or cloud OCR is not authorized. Then check whether this exact source hash already has an OCR job. Resume that job when present; otherwise submit once:

```sh
python scripts/ocr_first_adapter.py <run>/source.png --out-dir <run>/ocr-first
```

Do not resubmit when a prior submission result is uncertain. Follow [ocr-setup.md](ocr-setup.md) for a user-selected local or manual OCR route; do not describe manual transcription as cloud OCR.

## 3. Review every source text role before numbering

Create the editable review ledger from the saved OCR result; this reuses the existing OCR and makes no OCR request:

```sh
python scripts/text_roles.py draft --run-dir <run> \
  --raw-ocr <run>/ocr-first/ocr-candidates.json \
  --output <run>/text-role-review.json
```

The executing agent compares the entire source with each OCR text and box. Correct wording and boxes, split one OCR candidate that merged text from separate regions, and add visible OCR omissions as `visual_supplement` entries. Every raw OCR ID must be accounted for. Each entry records source text, source location and pixel box, `main_copy` or `illustration_internal`, and a reason based on the full image and narrative context. OCR block labels, words, character count, colors, and box shape are not role classifiers. Judge each entry by its narrative function in the full composition: a chart or progress graphic may contain reader-facing titles, explanations, or summaries that are main copy, alongside illustration-internal ticks, legend labels, object names, and status text. A sign-like prompt or card can also mix reader-facing copy with labels inside its pictured objects, so do not classify a whole frame as one role. Record each source location and contextual reason; never encode keyword, color, or container-type rules. Unclear entries stay pending; ask the user only when their intent or a source location cannot be determined. Software validates the bound ledger and its completeness claim but cannot prove that the agent noticed every visible pixel.

After the review is complete, compile it without resubmitting OCR:

```sh
python scripts/text_roles.py compile --run-dir <run> \
  --raw-ocr <run>/ocr-first/ocr-candidates.json \
  --review <run>/text-role-review.json \
  --candidates-out <run>/ocr-first/reviewed-candidates.json \
  --decisions-out <run>/decisions.json
```

Pending roles, missing source text/boxes, unaccounted OCR IDs, or missing review reasons block compilation and downstream work.

## 4. Show and record the partial selection

For partial mode, number only the reviewed `main_copy` items:

```sh
python scripts/selection_gate.py number-map --run-dir <run> \
  --ocr-candidates <run>/ocr-first/ocr-candidates.json \
  --role-review <run>/text-role-review.json
```

The preview is 2× the source size; mapped boxes stay on the original `source.png` pixel grid. Numbers are contiguous, ordered top-to-bottom and then left-to-right. The numbered preview and mapping bind the source, raw OCR, role-ledger, and preview hashes. Their file names include the role-ledger hash, so a changed review produces a fresh map and cannot reinterpret a prior number answer. Show users only each number, a short text excerpt, and the numbered image; keep OCR IDs, source boxes, and role reasons in the bound ledger.

After showing the map, use `functions.request_user_input_async` to offer A “按编号选择” and B “想拆的文字未被标记，发送截图”. The same question accepts a free-form number list (for example, `1,6,9`). If the user replies with numbers directly, record that selection and continue without asking them to confirm again. If they choose A without entering numbers, ask once for the numbers. For a number answer, record the method and the exact answer:

```sh
python scripts/selection_gate.py record-method --run-dir <run> --answer numbers
python scripts/selection_gate.py record-numbers --run-dir <run> --answer '1,6,9'
```

Accept half/full-width commas, enumeration commas, semicolons, or spaces. Empty, duplicate, invalid, out-of-range, stale-map, or stale-ledger answers are rejected; never default to all text.

For a screenshot answer, ask the user to attach one circled target in the ordinary conversation, then map it back to the original source pixels:

```sh
python scripts/selection_gate.py record-method --run-dir <run> --answer screenshot
python scripts/selection_gate.py record-screenshot --run-dir <run> \
  --screenshot <attached.png> --source-box 'x,y,width,height' --text '<verified source text>'
```

Bind the screenshot hash, source hash, exact source box, and verified wording. If text, position, or ownership remains ambiguous, keep the gate pending until the user clarifies. A screenshot that matches an already reviewed main-copy item is bound to that inventory item so it cannot create a duplicate editable layer. Overlap with another item needs explicit user resolution. The screenshot branch handles one target only; a new scope requires a new run.

## 5. Build the text plan

Generate the whole-image coverage aid using the compiled, source-reviewed candidates:

```sh
python scripts/text_first.py coverage-review --run-dir <run> \
  --ocr-candidates <run>/ocr-first/reviewed-candidates.json \
  --output <run>/coverage.json
```

Bind the coverage artifact into the compiled decisions with `text_roles.py bind-coverage`, using the source SHA-256 and the SHA-256 of `reviewed-candidates.json`. Then build the plan:

```sh
python scripts/text_first.py plan --run-dir <run> \
  --ocr-candidates <run>/ocr-first/reviewed-candidates.json \
  --decisions <run>/decisions.json --coverage-review <run>/coverage.json --require-ready
```

In full mode, reviewed main copy becomes `editable` or `title_raster`; illustration text stays `background_internal`. In partial mode, only selected main-copy items may become Type layers. Every unselected main-copy item, every illustration-internal label, and every visible manual supplement remains in the source-bound preserved inventory.

## 6. Review and import one complete background

Generate one full-canvas background candidate, not text-box patches or donor rectangles. For partial mode, list every selected item to clear and every preserved ledger item to retain by text and source location. See [imagegen-prompts.md](imagegen-prompts.md).

Compare the candidate visually with the complete source. Do not declare all text retained from a downscaled whole-image view alone: compare each ledger region at readable zoom side by side between source and candidate, paying special attention to OCR omissions and similar titles in different locations. Record the true result for each item. The partial acceptance record must cover every selected and preserved inventory ID exactly once with its source text and source box. Each preserved row must record `visible_in_source`, `retained_in_clean_base`, and `position_matches_source`; all three must be true. This includes unselected main copy, illustration labels, and OCR-missed visible text. Any missing row, false result, stale hash, or pending review blocks background readiness and Stage 3. `prepare_stage3.py` reopens the accepted review and checks the same role ledger again. The review is evidence for a human visual decision; JSON cannot prove that pixels were preserved.

```sh
python scripts/text_background.py --text-plan <text-plan.json> \
  --clean-base <accepted-full-canvas.png> \
  --background-review <accepted-background-review.json> \
  --foreground-exceptions <optional-reviewed-exceptions.json> \
  --out-dir <new-background-build>
```

If the one candidate fails, record the actual difference and stop. Do not enter local erase or threshold-repair loops.

## 7. Prepare, assemble, and inspect the PSD

Only create separate foreground assets for objects confirmed missing from the accepted background. Inventory first, group up to nine items per sparse sheet, and preserve source-to-cell-to-alpha bindings. See [asset-exceptions.md](asset-exceptions.md) and [artifact-contracts.md](artifact-contracts.md).

Create the style plan for editable text using reviewed source baselines and geometry. In partial mode, prepare rechecks the selection gate, main-copy roles, preserved inventory, and accepted background review:

```sh
python scripts/prepare_stage3.py --source <run>/source.png --text-plan <text-plan.json> \
  --background-build <background-build.json> --style-plan <style-plan.json> \
  --title-reviews <optional-title-reviews...> --out-dir <new-stage3-run>
python scripts/stage3_writer_adapter.py <new-stage3-run>/stage2/page_rebuild.json \
  --out-dir <new-stage3-run>/output
```

Omit `--title-reviews` when there are no title assets. Reopen the PSD with psd-tools and inspect its groups, native text, full preview, and important crops. Report automated structure checks separately from real visual checks. The user still owns Photoshop open/edit/save/close/reopen acceptance; until confirmed, status remains `PENDING_MANUAL_PHOTOSHOP`.
