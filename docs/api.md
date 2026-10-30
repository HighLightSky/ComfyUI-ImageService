# 图像编辑与姿势检索 API 接口文档

## 1. 地址和鉴权

本机测试地址为 `http://127.0.0.1:8000`，部署 HTTPS 后使用实际域名，例如 `https://image.example.com`。

以下 curl 示例共用这两个环境变量：

```bash
export API_BASE_URL='http://127.0.0.1:8000'
read -r -s -p '输入部署时配置的 API Token: ' IMAGE_SERVICE_API_TOKEN
export IMAGE_SERVICE_API_TOKEN
```

| 接口 | 用途 | 是否需要 Token |
|---|---|---|
| `GET /health` | FastAPI 进程存活检查 | 否 |
| `GET /ready` | ComfyUI、节点和模型文件就绪检查 | 是 |
| `POST /api/image/edit` | 上传参考图片和提示词，等待生成 PNG | 是 |
| `POST /api/image/edit/{task_id}/cancel` | 取消指定的排队或生成任务 | 是 |
| `POST /api/pose/search` | 按描述检索姿势，返回完整姿势 JSON | 是 |

需要鉴权时发送请求头：

```http
Authorization: Bearer <API Token>
```

Token 使用服务端 `IMAGE_SERVICE_API_TOKEN` 的值。调用者无需知道 ComfyUI 内部 Token，也不直接访问 8188。当前没有 `/docs` 或 `/openapi.json` 页面。

## 2. 存活检查：GET /health

```bash
curl "$API_BASE_URL/health"
```

成功返回 `HTTP 200`、`Content-Type: application/json`：

```json
{"status":"ok"}
```

客户端可以用此接口检查 FastAPI 是否运行；它不检查 ComfyUI 或 GPU，不能据此判断是否可以生图。

## 3. 就绪检查：GET /ready

```bash
curl --fail-with-body "$API_BASE_URL/ready" \
  -H "Authorization: Bearer $IMAGE_SERVICE_API_TOKEN"
```

成功返回 `HTTP 200`、`Content-Type: application/json`：

```json
{"status":"ready"}
```

尚未就绪返回 `HTTP 503`：

```json
{
  "error": {
    "code": "NOT_READY",
    "message": "ComfyUI、节点或模型尚未就绪"
  },
  "request_id": "2edc0f9a-ef81-4eae-87d7-677bd9c0ef91"
}
```

Token 错误返回 401。启动期间可间隔几秒重试 503；持续失败时由部署人员检查服务。此检查不执行推理，不保证显存充足，也不代表队列有空位。

## 4. 图像编辑：POST /api/image/edit

请求格式为 `multipart/form-data`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `images` | 重复的文件字段 | 至少一张 JPEG、PNG 或 WebP；按上传顺序保留前 10 张，其余丢弃 |
| `prompt` | UTF-8 字符串 | 必填且非空；直接用于图像编辑，不自动扩写 |
| `pose` | 可选 JSON 文本字段 | 最终静态姿态对象，默认最大 1 MiB；有此字段时启用姿态工作流 |

多张图片重复使用字段名 `images`，不要使用 `images[]`、`image_1` 等名字。第一张是编辑目标，其余是参考图；提示词可用 `<image1>`、`<image2>` 等指明对应图片。不要手动设置 multipart 的 `Content-Type` 请求头，让客户端生成 boundary。

带 `pose` 且只上传一张图片时，该图用于确定画幅并保留为 image2 人物参考，服务端渲染的人偶图作为 image1。
上传两张及以上图片时，image1 用于确定画幅并替换为人偶图；image2 及以后不重排、不增加图片。
image2 固定作为人物参考；其后图片按 Agent 原文说明使用。
尺寸沿用 EXIF 方向和 32 倍数对齐规则，不自动居中或调整关节、机位及缩放。服务不要求画幅图纯黑。
姿态必须是完整对象（如检索响应中的 `matches[i].pose`），不能传整个检索响应或服务端文件路径。
姿态中保存的旧提示词、截图和 capture_id 不参与执行，不重新检索或写回原库。

```bash
curl --fail-with-body "$API_BASE_URL/api/image/edit" \
  -H "Authorization: Bearer $IMAGE_SERVICE_API_TOKEN" \
  -F 'images=@canvas.png' \
  -F 'images=@character.png' \
  -F 'prompt=根据 image2 的人物生成清晨阳台场景，保持原有服装。' \
  -F 'pose=<final_pose.json;type=application/json' \
  --output result.png
```

`pose=<文件` 将内容作为文本字段发送；不要使用 `pose=@文件` 的文件上传形式。
后端代码可直接对最终姿态对象做 JSON 序列化后放入 `pose` 表单字段。
服务端只在 Agent 原文之前拼接以下固定说明，不调用 LLM 扩写，也不改写 image 编号：

```text
Draw character from image2
{Agent 原始提示词}
```

姿态请求沿用相同排队、任务 ID、取消及 PNG 响应机制；浏览器渲染也在同一任务取消链内。
普通编辑不加载姿态 LoRA、不添加前缀。姿态依赖缺失时不会退回普通生成；部署方法见 [姿态配置说明](configuration.md#环境变量配置)。
此模板沿用已跑通工作流；通用前缀在实测中影响了姿态跟随，因此固定 image2 的人物参考用途，详见 [姿态工作流说明](workflow.md#姿态渲染与工作流)。
姿态控制仍受种子和提示词影响，返回成功不保证每次生成都严格遵循姿态。

### curl 调用

在客户端当前目录执行；替换两张图片的路径，只上传一张时删除第二个 `images`：

```bash
if HTTP_STATUS=$(curl --silent --show-error \
  "$API_BASE_URL/api/image/edit" \
  -H "Authorization: Bearer $IMAGE_SERVICE_API_TOKEN" \
  -F 'prompt=将 <image2> 中的服装应用到 <image1> 的人物，保持人物身份和背景不变。' \
  -F 'images=@./input/reference-1.jpg' \
  -F 'images=@./input/reference-2.png' \
  --output response.body \
  --write-out '%{http_code}'); then
  if [ "$HTTP_STATUS" = '200' ]; then
    mv response.body result.png
    printf '图片已保存到 result.png\n'
  else
    printf 'HTTP %s\n' "$HTTP_STATUS"
    cat response.body
  fi
fi
```

`@` 后面是 **curl 所在机器上的文件路径**。例如从 ComfyUI 根目录运行时，`./input/a.jpg` 对应 `/home/liu/wzt/ComfyUI/input/a.jpg`，并不是 `/input/a.jpg`。文件无法读取时 curl 报错 26，请求尚未发送。

上述 `response.body` 和 `result.png` 都保存在客户端。示例按 HTTP 状态分别处理，避免把错误 JSON 当作 PNG；如果使用 `curl --fail-with-body --output result.png`，请求失败时也可能把错误正文写进这个文件。

### 成功响应

```http
HTTP/1.1 200 OK
Content-Type: image/png
Cache-Control: no-store
```

响应体为一张完整 PNG 的原始二进制字节。客户端可以直接作为图片显示、交给图像库解码，或保存为 `.png`；不要调用 `response.json()`，也不需要 Base64 解码或再次下载。

输出尺寸依据第一张图片，沿用工作流对齐到 32 倍数的规则，因此不保证与未对齐的原图逐像素同尺寸。一次请求只生成一张图，每次使用新的随机种子。

这是等待生成完成的请求，不返回下载 URL，也不提供进度或任务查询。需要主动取消时，调用方在提交前生成一个 UUID，通过可选请求头 `X-Task-ID` 传入；不传时保持原有行为。客户端关闭连接后，服务端仍会尝试取消并清理对应任务。

### Python 调用并区分两种返回结构

示例使用客户端的 `httpx` 包（可通过 `python -m pip install httpx` 安装）：

```python
import os
from contextlib import ExitStack
from pathlib import Path

import httpx

base_url = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")
token = os.environ["IMAGE_SERVICE_API_TOKEN"]
paths = [Path("input/reference-1.jpg"), Path("input/reference-2.png")]

with ExitStack() as stack, httpx.Client(timeout=httpx.Timeout(1800.0, connect=10.0)) as client:
    files = [
        ("images", (path.name, stack.enter_context(path.open("rb"))))
        for path in paths
    ]
    response = client.post(
        base_url + "/api/image/edit",
        headers={"Authorization": "Bearer " + token},
        data={"prompt": "参考 <image2> 修改 <image1> 的服装，保持人物身份不变。"},
        files=files,
    )

content_type = response.headers.get("content-type", "").split(";", 1)[0]
if response.status_code == 200 and content_type == "image/png":
    # 发生在调用方机器；也可以直接把 response.content 交给图像库。
    Path("result.png").write_bytes(response.content)
    print("图片已保存到 result.png")
elif content_type == "application/json":
    result = response.json()
    error = result.get("error", {})
    print(response.status_code, error.get("code"), error.get("message"), result.get("request_id"))
else:
    # Nginx 自身拒绝请求时可能返回 HTML 或其他格式。
    print("请求失败：", response.status_code, response.reason_phrase)
```

### 默认限制

实际限制由部署配置决定；当前默认值如下：

| 项目 | 默认值 |
|---|---|
| 保留图片数 | 前 10 张；没有图片则拒绝 |
| 单张保留图片大小 | 16 MiB |
| 整个请求大小 | 128 MiB，包含被丢弃的图片和 multipart 开销 |
| 单张图片像素数 / 最长边 | 4,194,304 像素 / 8192 像素 |
| 保留图片总像素数 | 16,777,216 像素 |
| 提示词长度 | 最多 16,384 个字符 |
| multipart 字段数 | 最多 64 个，包含 prompt 和被丢弃图片的字段 |
| 输出 PNG 大小 | 最多 64 MiB |
| 同时执行 / 等待队列 | 1 / 2；上传与响应传输也占用准入名额 |
| 上传 / 排队等待 / 执行上限 | 60 / 600 / 900 秒 |

超过 10 张不代表请求大小可以无限增加。多图请求可能等待数分钟，建议客户端读取超时至少 1800 秒。连接中断或超时后，不要立即无条件重复提交；新请求会重新生成，结果也可能不同。

## 5. 停止生成：POST /api/image/edit/{task_id}/cancel

提交生成请求时携带调用方生成的 UUID，每次生成使用不同的值：

```bash
TASK_ID=$(python -c 'import uuid; print(uuid.uuid4())')
echo "$TASK_ID"
curl "$API_BASE_URL/api/image/edit" \
  -H "Authorization: Bearer $IMAGE_SERVICE_API_TOKEN" \
  -H "X-Task-ID: $TASK_ID" \
  -F 'prompt=将背景修改为浅黄色，保留主体。' \
  -F 'images=@./input/reference-1.png' \
  --output response.body --write-out '%{http_code}\n'
```

生成请求等待期间，从另一条连接调用，使用相同的 Token 和 TASK_ID：

```bash
curl --fail-with-body -X POST "$API_BASE_URL/api/image/edit/$TASK_ID/cancel" \
  -H "Authorization: Bearer $IMAGE_SERVICE_API_TOKEN"
```

取消请求返回 `HTTP 202`、`Cache-Control: no-store`：

```json
{"task_id":"2edc0f9a-ef81-4eae-87d7-677bd9c0ef91","status":"cancelling"}
```

`202` 表示已接受取消，不表示 GPU 已立即停止。排队任务直接移出队列；执行任务沿用后端的定向取消及清理流程，不影响其他任务。原生成请求在取消处理结束后返回 `409 TASK_CANCELLED` 的错误 JSON，不能把该响应当作 PNG。

上传完成并入队后才可通过此接口取消；上传期间需要停止时，直接关闭原请求连接。任务不存在、上传尚未完成或任务已结束时返回 `404 TASK_NOT_FOUND`。UUID 无效返回 `400 INVALID_TASK_ID`；相同 UUID 的任务仍在排队、执行或清理时，重复提交返回 `409 TASK_ID_IN_USE`。此 ID 不提供结果恢复或幂等重试功能。

通过 Nginx 调用时，需要同步加入 `deploy/nginx.conf` 中的取消路由；该路由不占用生成接口的连接配额，队列满时仍能取消。

## 6. 错误结构与处理

应用错误通常返回 `Content-Type: application/json` 和以下结构：

```json
{
  "error": {
    "code": "QUEUE_FULL",
    "message": "服务繁忙，请稍后重试"
  },
  "request_id": "2edc0f9a-ef81-4eae-87d7-677bd9c0ef91"
}
```

| 字段 | 使用方法 |
|---|---|
| `error.code` | 用于程序分支判断的错误码 |
| `error.message` | 简短说明，可展示给调用者；不要依赖具体措辞写程序逻辑 |
| `request_id` | 本次失败请求的标识，用于反馈问题；不能用于查询或下载结果 |

| HTTP 状态 | 错误码 | 调用方处理 |
|---|---|---|
| 400 | `INVALID_MULTIPART`、`INVALID_PROMPT`、`INVALID_TASK_ID` | 检查表单内容或任务 UUID |
| 400 | `INVALID_POSE` | 检查 pose 是否为有效姿态 JSON 对象 |
| 401 | `UNAUTHORIZED` | 检查 Token 是否设置、是否与服务端一致 |
| 404 | `TASK_NOT_FOUND` | 任务尚未入队、不存在或已结束 |
| 408 | `UPLOAD_TIMEOUT` | 检查上传连接或减少本次数据量 |
| 409 | `TASK_ID_IN_USE` | 每次生成使用不同的任务 UUID |
| 409 | `TASK_CANCELLED` | 原生成任务已取消，无图片结果 |
| 413 | `REQUEST_TOO_LARGE`、`PART_TOO_LARGE`、`IMAGE_TOO_LARGE`、`TOO_MANY_PIXELS`、`TOO_MANY_PARTS`、`HEADERS_TOO_LARGE` | 减少大小、像素数或字段数，或联系部署人员调整限制 |
| 413 | `POSE_TOO_LARGE` | 姿态 JSON 或渲染 PNG 大小超限 |
| 422 | `INVALID_IMAGE`、`WORKFLOW_REJECTED` | 前者检查图片能否解码；后者由部署人员检查工作流 |
| 429 | `QUEUE_FULL`、`MEMORY_BUSY` | 降低并发，等待一段时间后再尝试 |
| 499 | `CLIENT_DISCONNECTED` | 客户端已取消或断开，通常收不到该响应 |
| 500 | `INTERNAL_ERROR` | 保留 request_id，反馈部署人员 |
| 502 | `COMFY_UNAVAILABLE`、`EXECUTION_FAILED`、`SERVICE_STOPPING`、`CLEANUP_FAILED` | 等待服务恢复并检查 `/ready`，持续失败时反馈部署人员 |
| 502 | `COMFY_PROTOCOL`、`WEBSOCKET_CLOSED`、`MISSING_OUTPUT`、`INVALID_OUTPUT`、`MULTIPLE_OUTPUTS` | 没有可用结果，反馈部署人员检查上游连接或输出 |
| 503 | `NOT_READY` | `/ready` 未就绪，稍后检查 |
| 503 | `POSE_NOT_READY` | 检查浏览器、Playwright、本地 VNCCS 资源、节点及姿态 LoRA |
| 502 | `POSE_RENDER_FAILED` | 检查姿态数据和渲染环境 |
| 504 | `POSE_RENDER_TIMEOUT` | 姿态渲染超时；取消和清理完成后可重试 |
| 504 | `QUEUE_TIMEOUT`、`EXECUTION_TIMEOUT` | 排队或执行超过上限，降低负载或联系部署人员 |

代理自身的 413、429、502、504 不保证使用以上 JSON 结构。先检查 HTTP 状态和 `Content-Type`；网络断开时也可能没有完整响应，不能将收到的部分数据视为成功 PNG。

## 7. 姿势检索：POST /api/pose/search

需要相同 Bearer Token。服务须先构建索引并配置 `IMAGE_SERVICE_POSE_INDEX_PATH`，见 [索引构建说明](poses.md#构建姿势检索索引)。

```bash
curl --fail-with-body "$API_BASE_URL/api/pose/search" \
  -H "Authorization: Bearer $IMAGE_SERVICE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"query":"坐在地上，双手撑在身后，上身后仰","top_k":3}'
```

请求体为 JSON，最大 64 KiB：`query` 为必填非空字符串，`top_k` 为正整数、默认 1。
超出所选库数量时返回该库全部姿势。可选 `library` 指定检索范围：

| `library` | 范围 |
|---|---|
| `default`（默认） | 原有姿势库，不包含 Mixamo 扩展 |
| `mixamo` | 仅 Mixamo 扩展库 |
| `all` | 原有库和 Mixamo 扩展库 |

例如 `{"query":"跑步","top_k":5,"library":"mixamo"}`。省略 `library` 的已有调用现在只检索原有库。
范围筛选发生在截取 Top-K 之前；所选库为空时返回 `{"matches":[]}`。
Mixamo 条目按姿势快照中的 `_library.source.dataset` 为 `Linzhan/Mixamo-Animations-Characters` 识别，无需重建已有索引。
不要上传向量、模型名或文件路径；查询编码使用服务端索引记录的模型和检索指令。

成功返回 `200 application/json`，设置 `Cache-Control: no-store`，结构始终为：

```json
{
  "matches": [
    {
      "pose_id": "local_user_poses/example/Seated.json",
      "score": 0.82,
      "description": {
        "zh": "人物坐在地上，双手撑在身后。",
        "en": "Sitting on the floor with both hands supporting the body from behind."
      },
      "pose": {
        "bones": {},
        "schema_version": 3
      }
    }
  ]
}
```

示例中的 `pose` 已简写；实际返回构建索引时的完整原始姿势 JSON，包括骨骼、角色及相机等字段。
`pose_id` 是库内相对路径标识，不是服务端绝对路径，也不是下载 URL。
按余弦相似度降序返回，同分按 `pose_id` 排序；分数不是置信概率，没有相似度拒绝阈值。
左右肢体、具体支撑关系等局部细节需对照返回描述确认，可用 `top_k` 获取多个候选。
此接口不调整姿势、不渲染预览、不触发生图。检索与生图队列独立，最多同时处理 2 个请求，超出立即返回 429。
查询只发送至已配置的 embedding 服务，不保存查询、查询向量或请求历史。

错误沿用第 6 节的 JSON 结构及 `request_id`：

| HTTP 状态 | 错误码 | 处理方式 |
|---|---|---|
| 400 | `INVALID_POSE_REQUEST` | 检查 JSON、query、top_k 或正文大小 |
| 401 | `UNAUTHORIZED` | 检查现有 API Token |
| 408 | `UPLOAD_TIMEOUT` | 检查客户端上传连接 |
| 429 | `POSE_SEARCH_BUSY` | 稍后重试 |
| 502 | `EMBEDDING_UNAVAILABLE` | 检查向量服务、认证、地址及 TLS 配置 |
| 502 | `EMBEDDING_PROTOCOL` | 向量服务响应格式、模型或维度异常，联系部署人员 |
| 503 | `POSE_INDEX_NOT_READY` | 检查索引文件、连接配置及模型和维度匹配，必要时重建后重启 API |
| 504 | `EMBEDDING_TIMEOUT` | 向量服务超时，稍后重试 |

原 `/ready` 仍仅表示图像服务就绪，不探测 embedding 服务或执行检索。
