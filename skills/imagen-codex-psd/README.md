# imagen-codex-psd 2.1.2

把一张 PNG、JPEG 或 WebP 海报整理成分层 PSD：完整背景、可编辑的主文案，以及背景清理后确实缺失时才补回的独立可移动素材。

开始时只需选择处理范围：

- **A：拆出全部主文案。** 插画内部的标签仍留在背景中。
- **B：只拆指定文案。** 先核对整张图的文字，再给主文案编号；看到编号图后，可在选择控件中按编号选择，也可以在同一题直接输入数字。如果想拆的文字未被标记，可选择截图入口并在普通对话中发一张圈出目标的截图。编号图出来之前不需要抄文案或猜编号。未选文字与插画标签保留在完整背景中。

流程会依次检查原图文字、生成并核对完整背景、确认是否有必须补回的素材、排入可编辑文字，最后交付 PSD 供你在 Photoshop 验证。复杂背景需要人工对照，不能承诺逐像素复原。

## 文字与版面

同一段主文案可在一个可编辑文字层中使用多种颜色、字号和字重；硬换行保留为原生段落，缓存预览与 PSD 使用同一布局框。当前垂直排版仅支持顶端对齐，不支持垂直居中。倾斜按源图可见基线检查，不能还原完整透视或剪切。

## 安全和依赖

需要 macOS、Python 3.10+、本机 Source Han Sans CN 字体和 Codex 内置图像工具。OCR 会把图片提交给 PaddleOCR 第三方服务，首次提交前会说明。若缺少凭证，可按 [OCR 设置说明](references/ocr-setup.md) 申请并通过 macOS Keychain 安全提示保存；不要把 token 发到聊天中。凭证检查只显示是否已配置和来源，不显示值，也不测试远程服务。

在隔离环境安装依赖并检查本机条件：

```sh
python3 -m venv /path/to/imagen-psd-venv
/path/to/imagen-psd-venv/bin/python -m pip install -r requirements.txt
/path/to/imagen-psd-venv/bin/python scripts/doctor.py
```

## 安装或升级

解压后先在包目录运行 `shasum -a 256 -c SHA256SUMS`。升级时先把 `~/.codex/skills/imagen-codex-psd` 备份到 `~/.codex/skill-backups/` 之类的目录，再复制包内 `imagen-codex-psd/` 到 `~/.codex/skills/imagen-codex-psd`。备份放在 Skills 目录之外；不要复制运行产物、图片或凭证。安装后重新打开 Codex，再检查技能版本。

## 2.1.2 更新

局部模式在编号图出来后保留“按编号选择”与“想拆的文字未被标记，发送截图”两个入口；直接回复数字即可继续，不重复确认。原生混合样式文字、按硬换行分段的段落范围及零 `SpaceBefore` 保持不变；垂直居中仍不支持。新版本不包含真实海报、用户 PSD、凭证或运行缓存。

历史 run 按其保存的来源和选择记录继续；新建的 run 不复用其他图片的 OCR 或文字坐标。自动结构检查不能代替 Photoshop 手动编辑、保存并重开确认。
