# Agent 调用

本文命令均在插件根目录执行，复用 ComfyUI 的 `../../.venv`。路径、用户名和服务地址按实际安装修改。

图片编辑请求必须提供 `images` 和 `prompt`。前 10 张图片按上传顺序连接 `images.image_1`～`images.image_10`，
其余图片丢弃但计入请求总大小及 multipart 字段数限制；默认最多 64 个字段（含 prompt），无图片返回 400。
编辑请求可额外提交完整 `pose` JSON：服务端将 image1 替换为人偶图，image2 固定为人物参考，其他图片编号不变。
只上传一张图片时，服务端按该图画幅渲染 image1，并将上传图保留为 image2 人物参考。
未包含 `pose` 时保留普通编辑行为；输入姿态不校正、不写回库，也不读取旧提示词。

### 最简单的网页测试

在插件根目录运行（仅用 Python 标准库，无需安装前端依赖）：

```bash
../../.venv/bin/python -m image_service.test_ui.server \
  --api-base-url https://10.21.16.104 \
  --ca-file /etc/ssl/qwen/lan-ca.crt \
  --port 8080
```

打开 `http://127.0.0.1:8080`，输入 API Token，选择图片、填写提示词后生成；可检查服务就绪、预览并下载 PNG。将 API 地址和 CA 公共证书路径替换为实际部署值，HTTPS 校验保持开启。Token 和图片不写入本地存储，只有点击下载时才保存结果。

也可输入姿势描述和候选数量 k（默认 5）查找姿势，无需先上传图片或填写生图提示词。
结果按相似度排列，展示中英文描述；点击“使用此姿态”会填入完整姿态 JSON 并预览，随后可按原流程生成。
此功能要求 API 已配置姿势索引和 Embedding 服务，配置方法见 [构建姿势检索索引](poses.md#构建姿势检索索引)。

需要其他局域网设备访问时，在启动命令追加 `--host 0.0.0.0`，访问 `http://服务器IP:8080`。此 HTTP 页面仅供可信局域网临时测试，Token 经该 HTTP 连接传给测试服务；测试结束后按 Ctrl+C 停止。

页面请求由独立测试服务转发到固定的 HTTPS API 地址，避免浏览器跨域限制，无需改动已有 443/API 配置。当前接口需要至少一张输入图片，不支持纯提示词文生图。该测试服务不提供任务进度或取消功能。

### curl 调用

```bash
curl --fail-with-body https://image.example.com/api/image/edit \
  -H "Authorization: Bearer $IMAGE_SERVICE_API_TOKEN" \
  -F 'prompt=将背景修改为浅黄色，保留主体。' \
  -F 'images=@reference-1.png' \
  -F 'images=@reference-2.png' \
  --output result.png
```

示例中图片读写发生在客户端，服务端不保存输入或输出。成功为 `200 image/png` 并设置 `Cache-Control: no-store`；失败为带 `error.code`、`error.message` 和 `request_id` 的 JSON。代理自身的拒绝（例如 413/429）仍可能是 Nginx 响应。

`GET /health` 只检查进程存活；`GET /ready` 需要相同 Bearer Token，检查内存扩展、必要节点接口及模型文件列表。就绪检查不执行 GPU 推理，因此不能证明显存足够或模型能够成功加载。

