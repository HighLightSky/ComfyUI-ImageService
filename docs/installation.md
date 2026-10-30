# 安装与启动

本文命令均在插件根目录执行，复用 ComfyUI 的 `../../.venv`。路径、用户名和服务地址按实际安装修改。

现有服务器已有依赖和资源时无需重复安装。新环境先按[权重与资源](models.md)下载默认工作流的三份权重，并确认 ComfyUI 支持 `TextEncodeQwenImage21`、`QwenImage21Cache` 和自带的 `SaveImageWebsocket` 节点。
当前启动器面向 Linux，Python 要求 3.12 或更新版本；无需修改 ComfyUI 核心代码。所有安装都在共用 `.venv` 中执行，不使用系统 Anaconda Python。

```bash
# 基础服务：先预览，确认后安装。
uv pip install --python ../../.venv/bin/python -r requirements.txt --dry-run
uv pip install --python ../../.venv/bin/python -r requirements.txt

# 需要姿态生图时：预览后安装包含基础依赖与 Playwright 的完整集合。
uv pip install --python ../../.venv/bin/python -r requirements-pose.txt --dry-run
uv pip install --python ../../.venv/bin/python -r requirements-pose.txt
# 仅用户明确需要时手工下载浏览器。
../../.venv/bin/python -m playwright install chromium --only-shell

# 每次安装后检查共享环境。
uv pip check --python ../../.venv/bin/python
```

首次安装 systemd 服务、Nginx 与 TLS 证书按[部署指南](deployment.md)操作；之后按上方配置环境变量和启停服务。
运行时不会自动安装依赖或下载模型、浏览器。

### 子项目依赖与维护入口

`pyproject.toml` 是直接依赖的唯一手工声明入口。`pose` 增加 Playwright，`dev` 增加 pytest、pytest-asyncio、httpx。
三份 requirements 由工具生成并提交 Git，锁定范围是 Ubuntu / Python 3.12 的服务依赖，不代表完整 ComfyUI、GPU 软件栈或 custom nodes 的环境。

```bash
# 修改声明后生成三份锁定文件；已有版本默认保留。
bash image_service/tools/lock_requirements.sh
# 有意升级单个包：bash image_service/tools/lock_requirements.sh --upgrade-package 包名

# 需要运行测试时，预览后安装基础 + pose + dev。
uv pip install --python ../../.venv/bin/python -r image_service/tests/requirements.txt --dry-run
uv pip install --python ../../.venv/bin/python -r image_service/tests/requirements.txt
uv pip check --python ../../.venv/bin/python
```

生成时使用 ComfyUI 根 requirements 作为约束；姿态与测试锁定文件继续约束前一份结果，确保共同依赖版本一致。
共享环境使用 `uv pip install`，不要执行 `uv sync` 或 `uv pip sync`。预览若涉及 GPU 软件栈或无关依赖，应先排查冲突。

| 任务 | 仓库根目录下的入口 |
|---|---|
| UI / API 启动 | `../../.venv/bin/python launch.py` / 追加 `--api` |
| 生成姿势描述 | `../../.venv/bin/python -m image_service.tools.describe_poses` |
| 构建姿势索引 | `../../.venv/bin/python -m image_service.tools.build_pose_index` |
| 转换工作流 | `../../.venv/bin/python -m image_service.workflow 源工作流.json` |
| 自动测试 | `../../.venv/bin/python -m pytest --rootdir=.. image_service/tests -q` |
| 临时测试页面 | `../../.venv/bin/python -m image_service.test_ui.server --help` |

