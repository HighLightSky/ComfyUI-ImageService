# 姿势库与维护工具

本文命令均在插件根目录执行，复用 ComfyUI 的 `../../.venv`。路径、用户名和服务地址按实际安装修改。

姿势工具源码快照位于 `image_service/vendor/qwen21-pose-skill/`，由当前仓库维护，没有嵌套 Git 仓库或 submodule。
来源提交和许可证见 [ORIGIN.md](../image_service/vendor/qwen21-pose-skill/ORIGIN.md)。

### 路径与资源

- `image_service/vendor/qwen21-pose-skill/scripts/`：姿势修改、迁移出图及检查脚本。
- `image_service/vendor/qwen21-pose-skill/workflows/`：随包 ComfyUI 工作流。
- `image_service/vendor/qwen21-pose-skill/pose_library/`：上游随包的 55 个精选姿势快照。
- `image_service/pose_library/`：插件维护工具默认使用的本机姿势库，迁移时已复制 556 个姿势；由 Git 忽略。
  新安装可从现有 VNCCS PoseLibrary 复制，或为描述工具显式指定 `--library`。脚本通过 `VNCCS_POSELIB` 访问本机副本。
- `models/loras/`（相对 ComfyUI 根目录）：实际姿势 LoRA，沿用现有模型目录。

工作流 JSON、渲染 HTML 和随包静态资源属于源码；标注、审核页和索引默认位于 Git 忽略的 `image_service/pose_annotations/`。
自定义输出目录应放在仓库外。`pose_downloads/`、`.pose-download-venv/`、模型、生成输出和已安装第三方节点不纳入源码。
vendor 脚本依赖按需安装，不并入服务基础依赖。

### 本地 CLI 使用

在插件根目录的 Bash 中运行：

```bash
source image_service/vendor/qwen21-pose-skill/env.sh

# 离线检查随包文件完整性
../../.venv/bin/python "$POSELAB/scripts/validate_package.py"

# 离线列出已安装的姿势
../../.venv/bin/python "$POSELAB/scripts/run_vnccs_ui.py" --list-poses

# ComfyUI 启动后检查节点与模型
../../.venv/bin/python "$POSELAB/scripts/doctor.py"
```

`env.sh` 从自身目录计算当前 ComfyUI 根路径，不依赖原仓库位置；
ComfyUI 地址、输入输出目录和姿势库路径可以预先通过对应环境变量覆盖。

`make_custom_pose.py` 需要运行中的 ComfyUI、VNCCS 节点和 Playwright 浏览器。
通过 `/vnccs/pose_library/list` 获取当前姿势 ID 后，可执行：

```bash
../../.venv/bin/python "$POSELAB/scripts/make_custom_pose.py" \
  --base "实际姿势ID或唯一名称" --list-bones
```

重复传入 `--rotate 骨骼名:轴编号:增量角度` 可以修改姿势，
配合 `--name`、`--category` 保存新姿势及预览。
完整参数见 `--help` 和随包安装指南。

当前 API 模式限制插件加载且不开放首页，不能直接供这些依赖前端的脚本使用。
这些 CLI 的上传、落盘和浏览器操作独立于 API 的内存处理流程。
在独立的普通 ComfyUI UI 会话中调试这些工具，不要替代 API 请求处理器。

### 生成待审核的姿势描述

独立脚本 `image_service/tools/describe_poses.py` 读取姿势预览图，调用指定视觉模型生成中英文描述。
默认处理已安装姿势库中的静态姿势；跳过动画及库配置，不需要启动 ComfyUI。
只生成待审核文件，不修改姿势 JSON、预览图或现有标签，也没有写回命令。

```bash
# 先生成 5 条，检查描述效果。
../../.venv/bin/python -m image_service.tools.describe_poses \
  --ca-file /path/to/ca.crt --limit 5

# 继续处理其余姿势，保留已完成及人工修改的描述。
../../.venv/bin/python -m image_service.tools.describe_poses \
  --ca-file /path/to/ca.crt

# 编辑 draft.json 后刷新本地审核页，不调用模型。
../../.venv/bin/python -m image_service.tools.describe_poses --render-only
```

运行前按[命令行维护工具配置](configuration.md#配置命令行维护工具)导出 `LLM_*`。HTTPS 内部 CA 用 `--ca-file` 指定，系统已信任时可省略。
每次请求只发送固定提示词和一张预览图，不发送姿势名称、路径或标签。
脚本复用现有 Pillow 和 Python 标准库，没有新增依赖；默认串行调用，单次超时 120 秒，
可用 `--timeout` 调整。只在手动运行该维护脚本时调用模型，不接入图像服务请求流程。

输出默认位于 `image_service/pose_annotations/`（Git 忽略）：

- `draft.json`：唯一可编辑来源。各条目中的 `description.zh`、`description.en` 为待审核文本；
  同时记录源文件相对路径、指纹、模型和提示词版本，不保存密钥、推理内容或完整模型响应。
- `review.html`：直接在本地浏览器打开，对照内嵌缩略图检查描述，无需服务器或外部资源。
  页面只供查看，修改文本请编辑 JSON 后执行 `--render-only`。

`--library` 可切换姿势库，`--output-dir` 可指定独立审核目录；输出目录不能位于姿势库内。
`--limit` 限制本次处理的未完成条目数量，续跑时只处理未完成或失败条目。
每条处理后原子保存 JSON；认证、TLS、连接或服务不可用错误会保留进度并停止。
其他条目错误记入 JSON，处理继续；有失败时退出码为 1。
源文件变化时保留旧描述并提示使用新输出目录重新标注；切换库、模型或提示词版本也需要新目录。
`--render-only` 从暂存文件读取原库位置，不需要重复提供 `--library`，也不校验模型配置。
描述经人工确认后，再单独安排写入 `_library.description`。

### 从 Mixamo FBX 转换静态姿势

`image_service/tools/import_mixamo_poses.py` 在本地浏览器中复用已安装 VNCCS 的骨架映射，
按全身关节位置差异选帧，输出静态姿势 JSON、同名 PNG 预览和来源报告。
描述由中英文动作标签与测量到的关节高度、弯曲情况组成，不调用视觉模型。
转换过程禁止网络访问；输入 FBX 与带 SHA256 清单的 FBXLoader 模块缓存必须事先准备好。
依赖见 `requirements-pose.txt`，另需本地 Chromium。

```bash
../../.venv/bin/python -m image_service.tools.import_mixamo_poses \
  --manifest pose_downloads/mixamo_first_batch/manifest.json \
  --source-dir pose_downloads/mixamo_first_batch/fbx \
  --dependencies pose_downloads/mixamo_first_batch/dependencies \
  --browser /path/to/chromium \
  --output-dir image_service/pose_annotations/mixamo_first_batch
```

输入清单的 `items` 包含 `file`、`sha256`、`category`、`label_zh`、`prompt`、`description`、
`motion_id`、`url`，顶层记录 `dataset`、`revision`、`license`。
依赖清单将原模块 URL 映射到本地 `file` 和 `sha256`。
默认每段最多保留 4 帧；`--min-distance` 是按站立身高归一化的关节位移均方根，默认 0.055，
跨文件去重，不额外生成机位或镜像变体来增加数量。
输出先放独立目录；检查预览后将 `library/local_user_poses/poses/Mixamo_*` 安装到实际姿势库，
再将描述合并到审核文件并构建索引。改变输入或去重设置时使用新输出目录。

首批新增 420 个姿势：日常 107、手势 69、舞蹈 128、运动 116。
原有 136 个姿势保留；本机审核页和来源报告位于 `pose_annotations/mixamo_first_batch/`。

### 构建姿势检索索引

`image_service/tools/build_pose_index.py` 将审核 JSON 中的中文描述编码为向量，保留中英文描述和完整姿势 JSON 快照。
与读图服务的 `LLM_*` 配置分开；不要求图像 API Token。

运行前按[命令行维护工具配置](configuration.md#配置命令行维护工具)导出 Embedding 变量，然后执行：

```bash
../../.venv/bin/python -m image_service.tools.build_pose_index
```

默认读取 `pose_annotations/draft.json`，输出 `pose_annotations/pose_index.npz`（相对 `image_service/`）。
可用 `--annotations`、`--output`、`--batch-size` 覆盖，默认每批 8 条、串行调用。
构建前检查所有条目的描述、唯一姿势路径及源文件指纹；存在缺失、失败或源文件变化时中止。
索引全量构建成功才原子替换，不修改描述、姿势 JSON 或预览图，构建失败保留旧索引。

索引使用 NumPy 非 pickle 格式，包含归一化 float32 向量、格式版本、模型名、维度、查询指令、描述指纹及姿势快照。
原库后续变化不影响已构建快照；描述、模型权重、量化方式或编码规则改变后，需要重新构建。

构建后在服务环境中设置索引绝对路径及匹配的模型、维度，再重启 API；见[环境变量配置](configuration.md#环境变量配置)。
原 version 1 的 Qwen3 索引在匹配配置下可继续使用。未启用、配置缺失、不匹配或索引损坏时仅检索返回 503。
启动时不调用模型，索引只加载一次，不热更新；普通编辑和 `/ready` 行为不变。
`POST /api/pose/search` 共用现有鉴权，默认返回一个匹配，支持 `top_k`；接口说明见 [API.md](api.md)。
默认只检索原有姿势库；请求中设置 `library: "mixamo"` 可检索 Mixamo 扩展库，`library: "all"` 可检索全部。测试页提供相同的范围选择。范围按索引中的来源信息划分，不需要重新生成向量。
检索直接返回原始完整 JSON，不调整姿势、不渲染预览、不触发生图，也不占用生图队列。
运行时只向配置的 embedding 服务发送查询，不保存查询或查询向量，不记录正文、密钥或上游原始响应。

