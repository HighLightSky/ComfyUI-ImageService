# 插件迁移与回退

本文命令均在插件根目录执行，复用 ComfyUI 的 `../../.venv`。路径、用户名和服务地址按实际安装修改。

## 插件安装与迁移

本仓库安装在 ComfyUI 的 `custom_nodes/ComfyUI-ImageService`，普通 ComfyUI 启动只注册 `MemoryImageInput` 和 `MemoryImageRelease`，不会自动启动 API，也不会限制 UI 路由。API 必须由 `launch.py --api` 单独启动。

首次安装：在 ComfyUI 的 `custom_nodes` 中 clone 本仓库并命名为 `ComfyUI-ImageService`，进入插件目录，再按下文安装依赖和配置。要求 Python 3.12 及支持 Qwen Image 2.1 节点的 ComfyUI；模型沿用 ComfyUI 的 models 目录。
clone 不包含模型、浏览器、本地姿势库或索引。姿态编辑还需已安装 VNCCS 的 web 资源和 LoRA；姿势检索需本地索引及匹配的 Embedding 服务配置。运行时不会自动下载这些资源。

本机从旧入口迁移时，原 `image_service/` 和 `launch_image_service.py` 保留为备份，旧 `ComfyUI-MemoryImageIO` 必须改为 `.disabled`，避免节点重复注册。插件使用独立 Git 仓库，不由 ComfyUI 父仓库跟踪。切换和回退步骤见[部署指南](migration.md#插件迁移与回退)。



以下入口用于本机从 ComfyUI 根目录旧版迁移。先复制最新源码、姿势库和标注索引，修正草稿的 library 路径，并完成插件测试；不重新生成向量。

在插件根目录审阅 `deploy/migrate_service.py` 和 service 模板后执行：

```bash
sudo ../../.venv/bin/python deploy/migrate_service.py \
  --https-url https://10.21.16.104 \
  --ca-file /etc/ssl/qwen/lan-ca.crt
```

脚本备份原单元和环境文件到 `/var/backups/qwen-image-api/时间戳/`，停止旧服务并禁用旧内存节点；以原服务用户执行 1/3/10 张图和普通/姿态/普通编辑的前台 GPU 验证。通过后更新启动入口和索引路径，启动 systemd 服务，检查三种检索范围及 HTTPS 生图，再重启验证。Token 保持原值，密钥不输出、不复制进仓库；Nginx 不修改。验证需要数分钟，期间 API 暂不可用。

任何步骤失败都会恢复原配置、旧节点并重启旧服务。成功后原根目录源码、数据与 `.disabled` 节点继续保留。稍后需要手工回退时，使用脚本打印的实际备份目录：

```bash
sudo systemctl stop qwen-image-api.service
sudo cp /var/backups/qwen-image-api/实际时间戳/qwen-image-api.service /etc/systemd/system/qwen-image-api.service
sudo cp /var/backups/qwen-image-api/实际时间戳/qwen-image-api.env /etc/qwen-image-api.env
mv ../ComfyUI-MemoryImageIO.disabled ../ComfyUI-MemoryImageIO
sudo systemctl daemon-reload
sudo systemctl start qwen-image-api.service
```

回退后新插件仍在 custom_nodes 中；普通 UI 启动前将本插件也改为 `.disabled`，避免两个同名节点同时注册。再次启用插件时同步禁用旧节点。

单独验证已运行部署（先在当前终端设置 API Token 及可选检索配置）：

```bash
../../.venv/bin/python -m image_service.tests.check_deployment \
  --url https://10.21.16.104 --ca-file /etc/ssl/qwen/lan-ca.crt --search --edit
```
