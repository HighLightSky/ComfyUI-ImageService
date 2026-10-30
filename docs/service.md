# 常用启停命令

本文命令均在插件根目录执行，复用 ComfyUI 的 `../../.venv`。路径、用户名和服务地址按实际安装修改。

### 后台 API 服务

当前使用 `qwen-image-api.service`，启动入口为 `launch.py --api`。
服务同时管理 FastAPI（默认 `127.0.0.1:8000`）和专用 ComfyUI（默认 `127.0.0.1:8188`）。

```bash
sudo systemctl start qwen-image-api.service       # 启动
sudo systemctl stop qwen-image-api.service        # 停止
sudo systemctl restart qwen-image-api.service     # 更新代码、环境变量或索引后重启
systemctl status qwen-image-api.service --no-pager
sudo journalctl -u qwen-image-api.service -n 50 --no-pager
```

停止该服务会同时停止两个子进程，Nginx 继续运行。不要单独杀 FastAPI 子进程，启动器会尝试重启。
`start` 不会设置开机自启。systemd 单元位于 `/etc/systemd/system/qwen-image-api.service`。

### Nginx

```bash
sudo systemctl start nginx
systemctl status nginx --no-pager
# 修改配置后，先在实际挂载命名空间中检查；通过后执行下一条重载命令。
sudo nsenter -t "$(systemctl show nginx -p MainPID --value)" -m -- nginx -t
sudo systemctl reload nginx
```

Nginx 专用于本 API 时，可用 `sudo systemctl stop nginx` 停止 HTTPS 入口；共享其他站点时保留 Nginx。
修改环境变量只需重启 API；修改 systemd 单元需先运行 `sudo systemctl daemon-reload`。

### 前台调试和测试页面

先停止后台 API，再启动普通 ComfyUI UI：

```bash
cd /home/liu/wzt/ComfyUI/custom_nodes/ComfyUI-ImageService
sudo systemctl stop qwen-image-api.service

# 普通 ComfyUI UI，原 main.py 参数可原样追加。
../../.venv/bin/python launch.py
```

需要前台 API 时，先退出普通 UI，导出下文所需的可选功能变量，再启动：

```bash
read -r -s -p '输入 API Token（至少 32 字符）: ' IMAGE_SERVICE_API_TOKEN
export IMAGE_SERVICE_API_TOKEN
../../.venv/bin/python launch.py --api

# API 测试页面
../../.venv/bin/python -m image_service.test_ui.server \
  --api-base-url https://10.21.16.104 \
  --ca-file /etc/ssl/qwen/lan-ca.crt \
  --port 8080
```

前台服务用 `Ctrl+C` 停止。UI 与 API 模式择一运行；8000 或 8188 已被占用时启动器报错，不会结束原服务。
测试页面是独立前台进程，启动命令见[测试页面](usage.md#最简单的网页测试)，同样用 `Ctrl+C` 停止；修改其 Python 代码后需重新启动。

