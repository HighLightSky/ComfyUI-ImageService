# 工作流与生命周期

本文命令均在插件根目录执行，复用 ComfyUI 的 `../../.venv`。路径、用户名和服务地址按实际安装修改。

`workflow_api.json` 已从所提供的前端子图转换，保留顶层实际的 W4A8 文本编码器、UNet、VAE、25 steps、CFG 1、Euler/Simple、Qwen 缓存设置。`resolution=0` 保留第一张图的尺寸规则并对齐至 32 的倍数。关闭提示词扩写，每次生成新随机种子。

需要重新从相同结构的 UI 工作流生成模板时：

```bash
../../.venv/bin/python -m image_service.workflow \
  ../../user/default/workflows/qwen_image_2_1_image_edit.json
```

该转换器针对本次提供的子图；不是通用前端图转换器。API 不接收任意工作流或模型选择参数。维护其他模型时，应同时更新模板、接口校验及测试。

上传基于 ASGI 流及 `MultipartParser` 数据回调，不使用 `UploadFile`、`request.form()`、标准 `/upload/image` 或临时文件。实际图像格式检查支持 JPEG/PNG/WebP，按 Pillow 解码内容而非文件扩展名判断；EXIF、RGB/透明图转换遵循现有 LoadImage 行为，动画图片只使用第一帧。图片尺寸超过配置边界时拒绝，不额外缩放。解码不阻塞 FastAPI 事件循环。

每次请求先占用有限准入位置，再接收正文；排队只保留 FastAPI 中的字节，获得执行位置后才上传到 ComfyUI 内存接口。随机 key 只读一次，任务、WS client_id 和 prompt_id 均隔离。内部接收有锁、字节预算、像素预算和过期回收。预算是应用管理的有效载荷及解码预留，不等同于 Python、图像库、模型和 GPU 的全部 RSS/显存上限。

默认 GPU 并发 1、等待队列 2、排队等待 600 秒、执行上限 900 秒。RTX 3090 实测 10 张 1024×1024 参考图约需 350 秒，因此将原方案的 300 秒排队等待调整为 600 秒，Nginx 等待时间相应设为 1800 秒；队尾请求仍可能因负载超过等待上限。其余大小和内存默认值见环境示例。

先建立 WS 再提交任务。只接收执行节点 503 的 PNG 帧，验证传统 8 字节协议头、PNG 完整性、执行成功及结束事件；不下载 `/history` 或 `/view` 的图片。

任务结束后提交模型节点加 `MemoryImageRelease` 的清理图。API 模式用 `--cache-classic` 的原生 `clean_unused()` 淘汰图片及解码结果缓存，保留相同模型节点缓存；确认清理完成后删除原任务和清理任务历史。清理失败使服务停止接收并退出，由启动器重启两进程。客户端断开或超时时精确取消指定任务，跟踪尚未完成的提交，确认任务离队后再清理；执行中的图像不会仅因 TTL 过期而释放。

### 进程管理

API 模式启动 `127.0.0.1:8000` 的单 worker FastAPI 和 `127.0.0.1:8188` 的专用 ComfyUI。仅白名单内存扩展与现有 WebSocket 输出扩展；关闭 Manager、资产系统及无关插件。ComfyUI 所有内部接口要求内部 Token，首页不开放。Hugging Face/Transformers 处于离线模式，不自动下载缺失模型。

子进程原始 stdout/stderr 丢弃，以防上游异常包含敏感内容；启动器仅输出启动/退出状态。启动器在依赖导入之前设置 `ORT_DISABLE_TELEMETRY=1` 和 `OTEL_SDK_DISABLED=true`；实测发现当前 ONNX Runtime 默认会创建遥测数据库，此设置避免其初始化。故障时检查 `/ready` 和 API 稳定错误码；不要在生产环境开启正文、提示词或原始异常日志。子进程退出后停止另一进程，启动器最多重试三次；systemd 另有启动速率限制。

API 子进程禁用 Python 字节码写入，临时目录设置为 `/dev/shm`，并继承启动器设置的 Core Dump 大小上限 0。`/dev/shm` 仍可能受 Swap 影响，系统边界见[部署说明](deployment.md#内存与隐私边界)。

### 姿态渲染与工作流

`pose_render.py` 使用异步 Playwright，固定白名单资源从本地供给，拒绝其他页面请求。
复用浏览器进程，每个任务创建独立 context 并在结束或取消时关闭；不保存 JSON、截图或渲染 PNG。
API 启动器的子进程 `TMPDIR` 保持为 `/dev/shm`，ComfyUI 使用独立的私有 tmpfs 临时目录，
退出时只清理该私有目录，不再清理工作区默认 `temp/`。
系统隐私边界见下文部署与审计要求。

姿态分支复用已验证工作流的模型、LoRA@1、gpu/int8 缓存和 25 步 Euler/Simple、CFG 1。
编码参考图分辨率为 1024，输出画幅由原 image1 确定。
按实测结果及确认后的协议，前缀使用原工作流的 `Draw character from image2`，image2 固定为人物参考，后续图片按 Agent 原文使用。
此前通用前缀在抬臂对照中未能跟随姿态，因此保留原工作流前缀。
v3 姿态应修改活动角色的 `characters[i].poses[activeTab]`；只修改顶层镜像 `bones` 不会覆盖活动姿态。
API 不自动完整取景，即使 JSON 指定的构图裁掉部分人物也按原样渲染。

