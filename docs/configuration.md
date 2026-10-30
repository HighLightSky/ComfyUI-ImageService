# 环境变量配置

本文命令均在插件根目录执行，复用 ComfyUI 的 `../../.venv`。路径、用户名和服务地址按实际安装修改。

### 按功能选择必填项

| 使用功能 | 提前准备的配置与资源 |
|---|---|
| 普通生图 | `IMAGE_SERVICE_API_TOKEN`；现有 ComfyUI、Qwen 模型和内存扩展 |
| 根据描述找姿势 | 在普通生图配置上增加 `IMAGE_SERVICE_POSE_INDEX_PATH` 和 Embedding 的 `URL`、`MODEL`、`DIMENSIONS`；索引必须存在 |
| 选择姿态后生图 | 在普通生图配置上增加 `IMAGE_SERVICE_POSE_BROWSER_PATH`；安装 Playwright、Chromium、VNCCS 本地渲染资源及姿势 LoRA |
| 维护工具生成描述 | `LLM_BASE_URL`、`LLM_MODEL`；不要求图像 API Token 或运行中的 ComfyUI |
| 维护工具构建索引 | Embedding 的 `URL`、`MODEL`、`DIMENSIONS`，以及已审核的描述；不要求图像 API Token |

Embedding 和描述服务的 URL 都是完整接口地址，不自动追加路径，没有默认地址、模型或密钥。
两者的 API Key 均按上游要求填写，留空时不发送 Authorization 头。HTTPS 始终校验证书。

### 配置后台服务

服务从 `/etc/qwen-image-api.env` 读取变量。程序不会自动读取仓库中的 `.env`，终端中的 `export` 也不会改变已运行的 systemd 服务。

```bash
# 仅在文件不存在时从模板创建，已有配置保持原样。
if ! sudo test -e /etc/qwen-image-api.env; then
    sudo install -m 600 deploy/service.env.example /etc/qwen-image-api.env
fi
sudoedit /etc/qwen-image-api.env
```

在文件中按需设置以下分组，格式为 `变量名=值`，不写 `export`。地址和密钥示例需替换为实际值。
模板及全部可调参数见 [service.env.example](../deploy/service.env.example)，配置定义和校验在 [image_service/config.py](../image_service/config.py)。

**普通生图：**

```ini
IMAGE_SERVICE_API_TOKEN=替换为至少32字符的随机Token
```

可用 `../../.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))'` 生成 Token，并妥善保管。
客户端填写同一个 Token。`IMAGE_SERVICE_INTERNAL_TOKEN` 通常不设置，由启动器每次随机生成；手工设置时也须至少 32 字符，且与外部 Token 不同。

**姿势检索：**以下模型和维度对应原有 Qwen3 / 4096 维索引。

```ini
IMAGE_SERVICE_POSE_INDEX_PATH=/home/liu/wzt/ComfyUI/custom_nodes/ComfyUI-ImageService/image_service/pose_annotations/pose_index.npz
IMAGE_SERVICE_POSE_EMBEDDING_URL=https://embedding.example.com/v1/embeddings
IMAGE_SERVICE_POSE_EMBEDDING_MODEL=Qwen3-Embedding-8B
IMAGE_SERVICE_POSE_EMBEDDING_DIMENSIONS=4096
IMAGE_SERVICE_POSE_EMBEDDING_API_KEY=
IMAGE_SERVICE_POSE_EMBEDDING_CA_FILE=/home/liu/.config/image-service/llm-server.crt
IMAGE_SERVICE_POSE_EMBEDDING_TIMEOUT=30
```

将 `embedding.example.com` 替换为实际地址。索引路径必须是存在的文件，模型和维度必须与索引一致。
`CA_FILE` 为内部 CA 公共证书路径；上游证书已被系统信任时留空。`TIMEOUT` 单位为秒，默认 30。
索引路径默认空，未启用时只有检索返回 503。索引只在服务启动时加载，重建后需重启 API。
已有有效索引可直接使用；没有索引时按[构建姿势检索索引](poses.md#构建姿势检索索引)操作。

`IMAGE_SERVICE_POSE_EMBEDDING_QUERY_INSTRUCTION` 只用于构建索引。需要原 Qwen 查询格式时设置：

```ini
IMAGE_SERVICE_POSE_EMBEDDING_QUERY_INSTRUCTION="Retrieve human pose descriptions that best match the requested body posture, limb positions, and contact relationships."
```

该值默认空。运行时以索引保存的指令为准：非空时编码 `Instruct: ...\nQuery: ...`，为空时直接编码查询。
修改环境中的指令不会改变已有索引，需要重新构建才能生效。`DIMENSIONS` 用于验证响应，不要求远程服务裁剪向量。

**姿态生图：**

```ini
IMAGE_SERVICE_POSE_BROWSER_PATH=/absolute/path/to/chrome-headless-shell
IMAGE_SERVICE_POSE_RENDER_TIMEOUT=30
IMAGE_SERVICE_MAX_POSE_BYTES=1048576
# 资源不在默认位置时才设置：
# IMAGE_SERVICE_POSE_ASSET_ROOT=/absolute/path/to/ComfyUI_VNCCS_Utils/web
```

`BROWSER_PATH` 必须指向真实的 Chromium headless shell 可执行文件，不能填缓存目录。安装后查找：

```bash
rg --files --hidden "$HOME/.cache/ms-playwright" -g chrome-headless-shell
```

浏览器版本升级后路径可能改变，应重新确认。浏览器路径默认空，未设置时姿态生图返回 `POSE_NOT_READY`。
`POSE_ASSET_ROOT` 默认是 ComfyUI 根目录下的 `custom_nodes/ComfyUI_VNCCS_Utils/web/`，必须包含渲染器白名单中的脚本、人体资源和贴图。
还需要 `models/loras/VNCCS_QI2_PoseStudioV1.1.safetensors`。API 渲染无需启用 VNCCS 插件、首页或浏览器调试端口。
浏览器和 CA 证书不要放在 ComfyUI 的 `temp/` 中，该目录可能被普通 UI 启动清理。

配置保存后执行：

```bash
sudo systemctl restart qwen-image-api.service
systemctl status qwen-image-api.service --no-pager
```

### 配置命令行维护工具

维护脚本不会自动读取 systemd 的环境文件。运行前在同一终端导出所需变量。
生成姿势描述只需要以下独立配置，不使用 Embedding 模型：

```bash
export LLM_BASE_URL='https://vision.example.com/v1/chat/completions'
export LLM_MODEL='实际视觉模型名称'
# 上游需要认证时输入密钥；无需认证则保持为空。
read -r -s -p '描述服务 API Key（可留空）: ' LLM_API_KEY
export LLM_API_KEY
```

内部 CA 用工具参数 `--ca-file /path/to/ca.crt` 指定，超时用 `--timeout`，默认 120 秒。
`--render-only` 仅刷新审核页，不需要模型连接配置。

构建索引需导出上文完整的 `IMAGE_SERVICE_POSE_EMBEDDING_*` 变量，取值应与部署一致。例如：

```bash
export IMAGE_SERVICE_POSE_EMBEDDING_URL='https://embedding.example.com/v1/embeddings'
export IMAGE_SERVICE_POSE_EMBEDDING_MODEL='Qwen3-Embedding-8B'
export IMAGE_SERVICE_POSE_EMBEDDING_DIMENSIONS=4096
export IMAGE_SERVICE_POSE_EMBEDDING_CA_FILE='/home/liu/.config/image-service/llm-server.crt'
read -r -s -p 'Embedding API Key（可留空）: ' IMAGE_SERVICE_POSE_EMBEDDING_API_KEY
export IMAGE_SERVICE_POSE_EMBEDDING_API_KEY
# 需要查询指令时，也导出 IMAGE_SERVICE_POSE_EMBEDDING_QUERY_INSTRUCTION。
```

经常使用时，可将维护工具所需的变量放入自己维护的可信 Bash 环境文件（权限 0600、仓库外），每次运行前加载：

```bash
set -a
source "$HOME/.config/image-service/tools.env"
set +a
```

前台 API 同样使用终端环境：导出 `IMAGE_SERVICE_API_TOKEN` 以及所需的姿势检索、渲染变量后，运行 `../../.venv/bin/python launch.py --api`。
终端变量只作用于随后启动的进程；它们不会同步回 `/etc/qwen-image-api.env`。

### 可选调优与常见错误

普通端口、队列、上传与像素限制、内存预算和超时均有默认值，通常不必提前配置：
`IMAGE_SERVICE_API_PORT=8000`、`IMAGE_SERVICE_COMFY_PORT=8188`、`IMAGE_SERVICE_QUEUE_SIZE=2`，
`IMAGE_SERVICE_QUEUE_TIMEOUT=600`、`IMAGE_SERVICE_EXECUTION_TIMEOUT=900`、`IMAGE_SERVICE_CLEANUP_TIMEOUT=30`。
其余参数及数值见[环境模板](../deploy/service.env.example)。修改端口时同步修改 Nginx；增加超时时同时检查 Nginx 和客户端的等待时间。
启动器会设置离线模式、内部 Token、临时目录及遥测禁用变量，无需用户重复配置。

| 现象 | 检查项 |
|---|---|
| 搜索返回 Nginx HTML 404 | 部署中的 Nginx 是否包含 `/api/pose/search` 路由；仓库模板的更新不会自动同步到 `/etc/nginx/` |
| `POSE_INDEX_NOT_READY` | 服务环境中是否设置索引路径、Embedding 地址、模型和维度；索引、CA 是否存在；修改后是否重启 API |
| `POSE_NOT_READY`，提示浏览器、依赖或资源未就绪 | `IMAGE_SERVICE_POSE_BROWSER_PATH` 是否为实际文件；Playwright 和 VNCCS 渲染资源是否齐全 |
| `POSE_NOT_READY`，提示姿态模型缺失 | 姿势 LoRA 文件是否存在，具体消息见 API 响应 |
| `EMBEDDING_UNAVAILABLE` / `EMBEDDING_PROTOCOL` / `EMBEDDING_TIMEOUT` | 上游地址、认证、CA、模型及返回维度、超时 |

`/health` 只检查进程存活，带 Token 的 `/ready` 只检查生图服务基础就绪，不探测 Embedding、不验证浏览器，也不执行 GPU 推理。

