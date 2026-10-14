# OCR 凭证和单次任务

## 第三方申请

本包保留已验证的 PaddleOCR-VL jobs API（模型 `PaddleOCR-VL-1.6`），不切换到新 SDK。OCR 会把图像提交给第三方服务，首次提交前先说明数据处理。用户已请求海报转换且未要求本地/保密处理时，必要的 OCR 阶段包含在转换授权中，不重复询问同一授权。用户明确要求本地/保密路线时改用该路线。若缺少 token，只询问一次是申请并安全配置、稍后继续，还是手工录入。访问 [AI Studio Access Token](https://aistudio.baidu.com/account/accessToken) 并登录，查看 [PaddleOCR 官方 API 文档](https://www.paddleocr.ai/latest/en/version3.x/inference_deployment/serving/paddleocr_official_api/overview.html)、[PaddleOCR-VL API 说明](https://ai.baidu.com/ai-doc/AISTUDIO/Vmkz2nz1p) 与[当前配额说明](https://ai.baidu.com/ai-doc/AISTUDIO/pmjcld5qm)。配额/价格依账号与官方当前页面为准；不要根据其他 Skill 的旧宣传假定免费量。

如果没有凭证，按当前授权工作流只询问一次：现在申请并配置、稍后继续，或选择手工录入。token 不放进聊天、命令参数或日志；优先在交互终端运行：

```sh
python scripts/ocr_credentials.py configure
python scripts/ocr_credentials.py status
```

`configure` 将 token 直接输入 macOS Keychain 提示，不作为子进程参数。`status` 只显示 `configured`、来源标签和 `live_service_validation=not_tested`。它不测试远端 API。旧环境变量和 `~/.editppt/config.yaml` 可供只读复用；状态输出只给来源名称，不给内容。将旧文件里的 token 迁移到 Keychain 后，用户可自行清理旧明文项。

## 请求规则

`ocr_first_adapter.py` 只处理名字为 `source.png` 的单张图。任务状态保存源 SHA-256、job id 和状态；恢复时只查询并继续相同 job。网络提交结果不明时不会自动发第二次 POST。OCR 服务会处理图像内容；在已有任务授权下说明用途并继续，任务未授权上传或用户要求本地/机密时暂停并询问路线。不要使用 OCR job 之外的“连通性测试图”消耗配额。

请求成功只说明服务返回结果，不表示 OCR 已校对或背景已清除。先核对文本、分类、坐标，再运行：

```sh
python scripts/text_first.py coverage-review \
  --run-dir /path/to/run \
  --ocr-candidates /path/to/run/ocr-candidates.json \
  --output /path/to/run/visual-coverage.json
```

这一步只画出 OCR 候选框，不运行检测器。打开原图和生成的 `whole-image-coverage-overlay.png` 检查全部画布，记录漏字、错字、非海报文字和分类。源图或 OCR 哈希变化后必须重做审查。整图覆盖还没有可接受依据时，计划保持 `pending_visual_review`。

## 无云凭证

用户明确选择离线手工路线后，可以用本地文字转录创建 `ocr-candidates.json`，其中 `backend` 设为 `manual`、`advisory_only` 设为 `false`，并绑定 coordinate source SHA-256 和每项 `box_px`。记录实际录入者与来源，不填写虚构 job id。随后同样生成整图覆盖预览、补录漏项并人工分类。输出中的 backend 必须仍标成 `manual`。
