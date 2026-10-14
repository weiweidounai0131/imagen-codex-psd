# 阶段输入合同

所有文件都绑定当前海报的 source SHA-256；相关阶段同时绑定 text-plan 和 clean-base SHA-256。以下是通用字段说明，不是示例海报。尖括号内容必须从本次运行读取/填写；不要复用其他海报的 IDs、哈希或坐标。

## 1. Accepted background review

传给 `text_background.py --background-review` 的 JSON 至少含：

```json
{
  "schema_version": 1,
  "status": "accepted",
  "reviewer": "<真实审查者>",
  "source_sha256": "<text plan绑定的坐标源哈希>",
  "text_plan_sha256": "<本次text-plan文件哈希>",
  "clean_base_sha256": "<完整清底PNG哈希>",
  "text_review": {
    "status": "accepted",
    "cleared_main_text_ids": ["<全部editable和title_raster IDs>"],
    "preserved_internal_text_ids": ["<全部background_internal IDs>"],
    "summary": "<主文案已清除且内部文字保持正确的整图结论>"
  },
  "nontext_structure_review": {
    "status": "accepted",
    "accepted_elements": ["<已检查的人物、框、图标、连线等对象描述>"],
    "missing_from_clean_base": [],
    "summary": "<结构关系与需要补回对象的结论>"
  },
  "review_basis": "<整图与局部上下文检查方法>",
  "scope_note": "<允许的轻微差异与硬性结构边界>"
}
```

程序要求 source/plan/base 三个哈希匹配；`text_review` 与 `nontext_structure_review` 都必须为 `accepted`，清除 ID 必须恰好覆盖全部 `editable`/`title_raster`，保留 ID 必须恰好覆盖全部 `background_internal`。`missing_from_clean_base` 列表定义可补的缺失对象 ID，不能把有轻微色差的对象误记为缺失。

若 text plan 是局部模式，`cleared_main_text_ids` 必须恰好等于选择集，`preserved_internal_text_ids` 必须恰好等于未选 OCR 集。另提供逐项审核记录：`cleared_selection_items` 含 `id`、`text`、`source_bbox_px`、`visible_in_source:true`、`removed_from_clean_base:true`；`preserved_selection_items` 含相同身份/框字段，以及 `visible_in_source:true`、`retained_in_clean_base:true`、`position_matches_source:true`。还必须提供 `illustration_text_identity:{checked:true,identities_preserved:true,summary:<实际视觉检查>}`。只有真实视觉审查可以填写这些通过标记；合成测试记录必须标明 `validation_kind:"synthetic_fixture_not_visual_acceptance"`，不得作为用户交付的背景验收。

成功导入的背景图逐像素等于输入 clean-base；这不代表 clean-base 与原图非文字区域逐像素相同。没有例外清单的海报可传空 `foreground_exceptions`。背景导入仍会把 PSD readiness 留为 false；标题或例外待办由各自状态单独决定。

## 2. Missing-object foreground exception

传给 `text_background.py --foreground-exceptions` 的 manifest 至少需要 `inputs.identity_source.sha256` 等于 source hash、`clean_base.sha256` 等于 accepted base hash，以及 `foreground_exceptions` 数组。资产表和处理文件有路径时同时绑定其 SHA-256：

```json
{
  "schema_version": 1,
  "status": "reviewed",
  "backend": {"type": "<builtin-imagegen-or-source-extraction>"},
  "inputs": {
    "identity_source": {"path": "<source image>", "sha256": "<source hash>"},
    "location_context": "<如何从source辨认对象位置>"
  },
  "clean_base": {"path": "<accepted clean base>", "sha256": "<base hash>"},
  "imagegen_output": {"workspace_asset_sheet": "<optional sheet path>", "workspace_sha256": "<sheet hash>", "generated_sha256": "<same sheet hash>"},
  "processing": {"alpha_asset_path": "<optional processed alpha path>", "alpha_asset_sha256": "<optional alpha hash>"},
  "foreground_exceptions": [{
    "id": "<safe unique ID>",
    "type": "missing_from_clean_base",
    "reason": "<为什么只在清底后缺失>",
    "source_bbox_px": "<整数[x,y,width,height]>",
    "alpha_asset_path": "<transparent asset path>",
    "alpha_asset_sha256": "<asset hash>",
    "visual_review": {"status": "approved", "reviewer": "<真实审查者>", "note": "<身份/轮廓检查>"},
    "placement": {"mode": "contain", "note": "<source锚点到目标框依据>", "z_order": "above clean background, below native text"}
  }]
}
```

每个 exception ID 必须与背景审查中的 `missing_from_clean_base` 一一对应；只接受此类型，不允许把背景已有对象再叠一层。路径可以相对 manifest。素材板按完整缺失清单分组，每板最多九项；0项不生板、1项一格、2–9项一张稀疏板、10项分9+1。复杂标题或确实无法清楚分隔时才单独开板并记录原因。

批量素材不使用一个全局 `processing.alpha_asset_path` 代表所有对象。每个 exception 自己保存独立 alpha 路径/hash、source bbox、cell ID/框和实际可见组件框；同一板的多个 exception 共享 `sheet_id`、原板与去底板 hash、cell map/split manifest hash，但 alpha 文件必须各自独立。`asset_sheet_ref` 至少为：

```json
{
  "sheet_id": "<本次分组ID>",
  "cell_id": "<必须等于当前 exception ID>",
  "generated_sheet_path": "<原始素材板>",
  "generated_sheet_sha256": "<原板SHA-256>",
  "processed_sheet_path": "<去底后的RGBA素材板>",
  "processed_sheet_sha256": "<去底板SHA-256>",
  "cell_map_path": "<逐ID单元格映射>",
  "cell_map_sha256": "<映射SHA-256>",
  "split_manifest_path": "<逐对象分离报告>",
  "split_manifest_sha256": "<报告SHA-256>",
  "cell_bbox_px": [0, 0, 0, 0],
  "component_bbox_px": [0, 0, 0, 0]
}
```

每个 exception 另有 `alpha_asset_path`、`alpha_asset_sha256` 和自己的 `source_bbox_px`。程序校验 ID→格子→组件→独立alpha的路径、尺寸、像素及hash链；板像素尺寸不是PSD显示尺寸。不要把一份透明图复制为多个 ID。

## 3. Raster-title review

`prepare_stage3.py --title-reviews` 可传零个或多个标题 review JSON。每个 `title_raster` 计划项必须恰好有一个 `status:"accepted"` 的 review，至少绑定真实 `reviewer`、`source_sha256`、`text_plan_sha256`、`clean_base_sha256`、`text_item_id`、完整 `text`、`source_bbox_px`、`asset_path` 和 `asset_sha256`。文本与框必须等于本次 plan，资产按 review 文件相对路径解析并重算 hash。没有标题时省略该参数。

review 中可额外保存 `visual_review`、`asset_alpha_review`、生成/提取方法等证据。机器 hash 检查不能证明边缘干净或字形准确；人工审查真实 alpha 与 contain-fit 预览。标题层验收与整图清底验收分开。

## 4. Style plan

根对象必须有 `schema_version:2`、`source_sha256`、`text_plan_sha256` 和 `styles`。styles 的 `text_item_id` 集合必须恰好等于所有 `classification:"editable"` 的 text IDs。OCR adapter 保留 `source_geometry` 的全部合法坐标点：四点凸四边形可给出一个角度建议；其他多点轮廓只作为形状参考，不截断、不从前四点猜角。建议角度不是人工批准。每个 style 都要对照源图本项实际文字基线完成明确审查，包括水平项；缺少审查就保持 pending 并拒绝组装。每项至少提供：

```json
{
  "text_item_id": "<editable ID>",
  "display_lines": ["<line one>", "<line two>"],
  "layout_box_px": "<整数[x,y,width,height]，在画布内>",
  "font_weight": "<ExtraLight|Light|Normal|Regular|Medium|Bold|Heavy>",
  "font_size_pt": "<正数>",
  "color_rgba": "<3或4个0到255整数>",
  "style_runs": [
    {"text": "<连续且不重复的原文片段>", "font_weight": "<weight>",
     "font_size_pt": "<正数>", "color_rgba": "<3或4个0到255整数>"}
  ],
  "alignment": "<left|center|right>",
  "vertical_alignment": "top",
  "tracking": "<整数>",
  "auto_leading": "<true|false>",
  "leading_pt": "<关闭auto leading时的正数>",
  "rotation_deg": 0.0,
  "geometry_review": {
    "status": "reviewed",
    "reviewer": "<真实审查者>",
    "basis": "<source_baseline_visual_review 或 quad_suggestion_plus_visual_review>",
    "reviewed_rotation_deg": 0.0,
    "note": "<本项文字行的可见基线证据；多行逐行检查>"
  },
  "layout_review": {"status": "reviewed_by_author", "note": "<本项排版检查依据>"}
}
```

`display_lines` 重新连接后必须保留 source text，不得增删改字。`layout_box_px` 是文字可排版净空，可以不同于 OCR 墨迹框。当前原生文字只支持 `vertical_alignment:"top"`；省略时沿用 `top` 默认以兼容旧 plan。居中状态在 Photoshop 手动编辑/重排验收前不支持，也不得用缓存内移或段落 `SpaceBefore` 仿真。原生 TySh bounds 记录 layout box；缓存保持 writer 的原始文字图像并依据相同 layout box 放置。结构回读只核对布局框、变换和段落字段，不证明 Photoshop 编辑重排后位置或字形基线一致。

`style_runs` 是可选字段。省略时，整项继续使用原有的单一字体、字号和颜色。提供时，每段都必须包含非空 `text`、受支持的 `font_weight`、正数 `font_size_pt` 和 `color_rgba`；可选 `tracking` 继承整项 tracking。按顺序连接所有片段必须与 `display_lines` 用 `\n` 连接后的文字完全相同，包含原有空格和换行；不得漏字、重字、换序或把父文案拆成重复 Type 层。每个 `text_item_id` 仍只对应一个原生 Type 图层。一个图层内各片段自动共用父项的 layout box、文字基线、旋转角度和旋转锚点；不要在片段上另设几何变换。预览按同一行共同基线绘制不同字号，原生 TySh 的 `StyleRun` 写入对应字号、颜色和字重。显式关闭 auto leading 时，所有片段使用父项同一个 `leading_pt`，单位为点；在 72 ppi 画布中预览使用同数值像素作为相邻行基线间距。原生 `ParagraphRun` 每个 CR 换行对应一个段落范围，范围长度按 UTF-16 计算并包含该段末尾 CR；连续 CR 会产生空段落，其范围长度为一个 UTF-16 单元。所有段落 `SpaceBefore` 固定为 0，避免额外首段补偿。`StyleRun` 与 `ParagraphRun` 都覆盖整个 `Editor.Text`，包括 emoji 的代理对、硬换行和文末 CR；重开 PSD 后会逐段核验。结构回读不证明 Photoshop 的编辑/重排行为，仍需人工验收。

确属同一条基线、拆成不同颜色/样式的文字可共同填写 `rotation_group_id` 与同一个 `rotation_anchor_px:[x,y]`；组内角度和锚点必须完全一致。不得为了方便把不同文本行归到同一组。writer 用同一角度/锚点变换文字缓存和原生 TySh；多点OCR轮廓不是共享锚点依据。背景透视/剪切不在本版本变换范围内，旋转只近似恢复行基线。Photoshop 打开后的编辑观感仍需人工确认。

## 5. 组装状态

`prepare_stage3.py` 要求每个标题 review 集合与 plan 中全部 `title_raster` IDs 相等；前景 manifest ID 集合须与 background build 已包含的 IDs 相等，pending exceptions 必须为空。所有引用都需 hash 匹配。结构检查成功仅是自动验证；新 PSD 还需用户 Photoshop 打开/编辑/保存/重开，未确认时保持 `PENDING_MANUAL_PHOTOSHOP`。
