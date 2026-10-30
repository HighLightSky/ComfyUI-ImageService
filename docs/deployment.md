# 部署步骤

适用于当前 Ubuntu 服务器，项目目录为 `/home/liu/wzt/ComfyUI`，运行用户为 `liu`。现有 ComfyUI 和 Qwen 模型应已能正常生图；无需重新下载模型。以下命令在服务器插件目录执行；Python 复用 ComfyUI 的虚拟环境。

## 1. 安装 API 依赖

按 [安装与启动](installation.md#安装与启动) 预览并安装锁定依赖，随后执行依赖检查；可选依赖见同页维护入口。

```bash
cd /home/liu/wzt/ComfyUI/custom_nodes/ComfyUI-ImageService
ss -ltnp '( sport = :443 or sport = :8000 or sport = :8188 )'
```

如果 8000 或 8188 已被手动启动的 API / ComfyUI 占用，先在原终端按 `Ctrl+C` 关闭。443 如果已有站点，后续将配置合并到现有 Nginx，保留其他站点。

## 2. 仅在本机启动测试

不需要 Nginx。第一个终端执行：

```bash
cd /home/liu/wzt/ComfyUI/custom_nodes/ComfyUI-ImageService
read -r -s -p '输入 API Token（至少 32 个字符）: ' IMAGE_SERVICE_API_TOKEN
export IMAGE_SERVICE_API_TOKEN
../../.venv/bin/python launch.py --api
```

需要生成随机 Token 时，可先执行下面的命令，并保管生成结果：

```bash
../../.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

第二个终端输入同一个 Token，检查服务：

```bash
read -r -s -p '输入同一个 API Token: ' IMAGE_SERVICE_API_TOKEN
export IMAGE_SERVICE_API_TOKEN
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready \
  -H "Authorization: Bearer $IMAGE_SERVICE_API_TOKEN"
```

分别返回 `{"status":"ok"}` 和 `{"status":"ready"}` 后，按[接口文档](api.md)测试生图。启动初期 `/ready` 可能返回 503，稍后重试。

只做本机测试，到这里即可；停止时在第一个终端按 `Ctrl+C`。不加 `--api` 则启动常规 ComfyUI UI。

## 3. 配置 systemd 常驻服务

先停止第 2 步的手动服务，再创建环境文件：

```bash
cd /home/liu/wzt/ComfyUI/custom_nodes/ComfyUI-ImageService
if ! sudo test -e /etc/qwen-image-api.env; then
    sudo install -m 600 deploy/service.env.example /etc/qwen-image-api.env
fi
sudoedit /etc/qwen-image-api.env
```

把 `IMAGE_SERVICE_API_TOKEN=` 填为准备好的随机 Token，至少 32 个字符。其他参数可先保留默认值；内部 Token 无需填写，由启动器自动生成。程序不会自动读取项目目录中的 `.env`。

安装并启动服务：

```bash
sudo install -m 644 deploy/qwen-image-api.service /etc/systemd/system/qwen-image-api.service
sudo systemctl daemon-reload
sudo systemctl start qwen-image-api
sudo systemctl status qwen-image-api --no-pager
```

如果部署路径或用户不同，先编辑 `/etc/systemd/system/qwen-image-api.service` 中的 `User`、`WorkingDirectory` 和 `ExecStart`，再启动。这里只启动服务，不设置开机自启。重新执行第 2 步的两个 curl，确认 `/ready` 返回 200。

## 4. 为局域网地址生成 TLS 证书并安装 Nginx

使用局域网 IP 访问，例如 `https://192.168.1.100`。不需要公网 IP、域名或公网证书申请；下面使用本地 CA 签发服务器证书，由客户端信任这个 CA。

### 4.1 确认局域网地址并安装工具

```bash
ip -br -4 addr show scope global
```

找到其他局域网设备能访问的网卡 IP。下文统一以服务器 `192.168.1.100`、局域网网段 `192.168.1.0/24` 为例，执行时替换为实际值。建议通过路由器 DHCP 地址保留或静态配置固定服务器 IP。

尚未安装工具时执行；已经安装则跳过：

```bash
sudo apt-get update
sudo apt-get install -y nginx openssl
```

### 4.2 创建本地 CA（首次部署执行一次）

在服务器执行：

```bash
sudo install -d -m 755 /etc/ssl/qwen
sudo install -d -m 700 /etc/ssl/qwen/ca
sudo openssl req -x509 -newkey rsa:3072 -noenc -sha256 -days 3650 \
  -subj '/CN=Qwen LAN Root CA' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -addext 'subjectKeyIdentifier=hash' \
  -keyout /etc/ssl/qwen/ca/lan-ca.key \
  -out /etc/ssl/qwen/lan-ca.crt
sudo chmod 600 /etc/ssl/qwen/ca/lan-ca.key
sudo chmod 644 /etc/ssl/qwen/lan-ca.crt
```

`lan-ca.crt` 是提供给客户端的 CA 公共证书；`ca/lan-ca.key` 是签发证书的 CA 私钥，仅保留在受控服务器或管理员备份中，不分发给客户端。已有这套 CA 时复用它，不要每次部署都重新生成。

### 4.3 签发服务器证书

在同一个服务器终端设置实际 IP，并生成服务器私钥和签名请求：

```bash
export IMAGE_SERVICE_LAN_IP='192.168.1.100'
sudo openssl req -new -newkey rsa:3072 -noenc -sha256 \
  -subj "/CN=$IMAGE_SERVICE_LAN_IP" \
  -keyout /etc/ssl/qwen/privkey.pem \
  -out /etc/ssl/qwen/server.csr
sudo chmod 600 /etc/ssl/qwen/privkey.pem
```

生成包含该 IP 的 SAN 扩展，再用本地 CA 签发有效期为一年的证书：

```bash
sudo tee /etc/ssl/qwen/server.ext >/dev/null <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:$IMAGE_SERVICE_LAN_IP
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
sudo openssl x509 -req -sha256 -days 365 \
  -in /etc/ssl/qwen/server.csr \
  -CA /etc/ssl/qwen/lan-ca.crt \
  -CAkey /etc/ssl/qwen/ca/lan-ca.key \
  -CAcreateserial \
  -extfile /etc/ssl/qwen/server.ext \
  -out /etc/ssl/qwen/fullchain.pem
sudo chmod 644 /etc/ssl/qwen/fullchain.pem
openssl verify -CAfile /etc/ssl/qwen/lan-ca.crt \
  -purpose sslserver -verify_ip "$IMAGE_SERVICE_LAN_IP" \
  /etc/ssl/qwen/fullchain.pem
```

最后应显示 `/etc/ssl/qwen/fullchain.pem: OK`。证书必须在 `subjectAltName` 中包含实际访问的 IP，只填写 CN 不够；命令参数见 [OpenSSL 证书签发说明](https://docs.openssl.org/master/man1/openssl-x509/)。

此时 Nginx 所需文件已经放好：

| 文件 | 用途 |
|---|---|
| `/etc/ssl/qwen/fullchain.pem` | 服务器证书；本例由根 CA 直接签发，无需附加中间证书 |
| `/etc/ssl/qwen/privkey.pem` | 服务器私钥，权限 600 |
| `/etc/ssl/qwen/lan-ca.crt` | 客户端需要信任的 CA 公共证书 |

## 5. 内存临时目录随 Nginx 启停

`/run/qwen-nginx/body` 和 `/run/qwen-nginx/proxy` 是 Nginx 临时缓冲的保护目录；正常图片输入输出仍通过进程内存传递，FastAPI 不把图片存进这些目录。只使用本机 8000 端口时不需要这一步。

使用第 6 步的 systemd 配置管理生命周期，无需修改 `/etc/fstab`、设置开机挂载或手动创建子目录：

- Nginx 启动时，systemd 创建 `/run/qwen-nginx`，并在 Nginx 的私有挂载命名空间中挂载最多 64 MiB 的 `tmpfs`；Nginx 初始化时创建 `body` 和 `proxy`。
- Nginx 进程退出后，私有挂载释放，systemd 删除运行目录；下次启动重新创建。
- `noswap` 禁止该挂载使用 Swap。普通 tmpfs 默认可以使用 Swap，见 [Linux 内核说明](https://docs.kernel.org/filesystems/tmpfs.html)。

该目录跟随实际使用它的 Nginx，而不是单独跟随 FastAPI。Nginx 专用于本 API 时，按第 8 步一起启停；如果 Nginx 还服务其他站点，停止 API 时保留 Nginx 及其运行目录。

如果已经执行旧版步骤，先在允许中断 Nginx 的时间停止它，从 `/etc/fstab` 删除之前添加的 `/run/qwen-nginx` 那一行，删除 `/etc/tmpfiles.d/qwen-nginx.conf`，并卸载旧挂载，再按第 6 步配置。不要改动其他挂载或其他站点的目录。

## 6. 配置局域网 HTTPS 反向代理

```bash
cd /home/liu/wzt/ComfyUI/custom_nodes/ComfyUI-ImageService
sudo install -m 644 deploy/nginx.conf /etc/nginx/conf.d/qwen-image-api.conf
sudoedit /etc/nginx/conf.d/qwen-image-api.conf
```

将 `server {}` 开头的监听地址、名称和证书配置改为下面这样，并加入局域网访问限制。IP 和网段都要替换为实际值，IP 必须与第 4 步证书一致：

```nginx
server {
    listen 192.168.1.100:443 ssl;
    server_name 192.168.1.100;
    ssl_certificate /etc/ssl/qwen/fullchain.pem;
    ssl_certificate_key /etc/ssl/qwen/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    allow 192.168.1.0/24;
    deny all;

    # 后续保留示例已有的缓冲、超时、限流和 location 配置。
}
```

这是配置开头的示意，不要用它覆盖整个文件。该文件包含 `limit_conn_zone` 和 `limit_req_zone`，应放在 Ubuntu 默认由 `http {}` 引入的 `conf.d` 中，不要再包一层 `http {}` 或塞入其他 `server {}`。FastAPI 的 8000 和 ComfyUI 的 8188 仍只监听 `127.0.0.1`。

保留示例关闭请求/响应缓冲、磁盘缓存和临时响应文件的设置，以及 `proxy_http_version 1.1`；后者也影响 chunked 请求的缓冲行为，见 [Nginx 官方说明](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_request_buffering)。

为 Nginx 设置随进程创建和清理的内存目录，并禁止 Swap 和 Core Dump：

```bash
sudo install -d /etc/systemd/system/nginx.service.d
sudo tee /etc/systemd/system/nginx.service.d/qwen-memory.conf >/dev/null <<'EOF'
[Service]
RuntimeDirectory=qwen-nginx
RuntimeDirectoryMode=0755
TemporaryFileSystem=/run/qwen-nginx:rw,nosuid,nodev,noexec,noswap,size=64M,mode=0755
MemorySwapMax=0
LimitCORE=0
EOF
sudo systemctl daemon-reload
sudo systemctl restart nginx
sudo systemctl status nginx --no-pager
```

这里先由 systemd 准备目录和挂载，再由 Ubuntu 的 Nginx 服务执行启动前配置检查。重启会影响现有 Nginx 连接，应在允许短暂重启的时间执行。`RuntimeDirectory` 和私有 tmpfs 的行为见 [systemd 官方说明](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#TemporaryFileSystem=)。

启动后，在 Nginx 的挂载命名空间内检查目录和配置：

```bash
sudo nsenter -t "$(systemctl show nginx -p MainPID --value)" -m -- findmnt /run/qwen-nginx
sudo nsenter -t "$(systemctl show nginx -p MainPID --value)" -m -- nginx -t
```

确认挂载为带 `noswap` 的 `tmpfs`。此挂载只在 Nginx 内可见，普通终端中的 `findmnt` 看不到它。后续修改配置时，执行上述命名空间内的 `nginx -t`，通过后再执行 `sudo systemctl reload nginx`。

如果 Nginx 专用于此 API，且此前已设置过开机自启，可以关闭自启；这不会停止当前正在运行的服务：

```bash
sudo systemctl disable nginx qwen-image-api
```

## 7. 配置局域网访问、客户端信任并验证

### 7.1 仅允许局域网连接

不配置公网入口或路由器端口转发。如果正在使用 UFW，只允许实际局域网网段访问服务器的 443：

```bash
sudo ufw allow proto tcp from 192.168.1.0/24 to 192.168.1.100 port 443
```

如果此前专门为这个服务添加了 `ufw allow 443/tcp` 全开放规则，确认不影响其他站点后，用 `sudo ufw delete allow 443/tcp` 删除它。8000、8188 不开放给其他局域网设备。

### 7.2 将 CA 公共证书复制到客户端

下面命令在调用 API 的客户端执行，替换服务器 IP 和 SSH 用户；使用已确认可信的 SSH 连接：

```bash
scp liu@192.168.1.100:/etc/ssl/qwen/lan-ca.crt ./qwen-lan-ca.crt
openssl x509 -in ./qwen-lan-ca.crt -noout -fingerprint -sha256
```

在服务器执行 `openssl x509 -in /etc/ssl/qwen/lan-ca.crt -noout -fingerprint -sha256`，通过可信渠道核对两端指纹一致。客户端只需要 `.crt` 公共证书，不需要任何 `.key` 或 `privkey.pem` 文件。

### 7.3 指定 CA 验证 HTTPS

在客户端执行：

```bash
export API_BASE_URL='https://192.168.1.100'
export NO_PROXY="192.168.1.100,127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
read -r -s -p '输入 API Token: ' IMAGE_SERVICE_API_TOKEN
export IMAGE_SERVICE_API_TOKEN
curl --cacert ./qwen-lan-ca.crt "$API_BASE_URL/health"
curl --cacert ./qwen-lan-ca.crt "$API_BASE_URL/ready" \
  -H "Authorization: Bearer $IMAGE_SERVICE_API_TOKEN"
```

`NO_PROXY` 让该内网地址直连，避免继承客户端的 HTTP(S) 代理。分别返回 `{"status":"ok"}` 和 `{"status":"ready"}` 后，按[接口文档](api.md)提交图片：将地址改为局域网 IP，并给 curl 命令加上 `--cacert ./qwen-lan-ca.crt`。不要使用 `curl -k` 绕过校验。证书不受信任时检查 CA 文件；提示 IP 不匹配时检查访问地址是否与证书 SAN 一致。

Python/httpx 客户端使用接口文档示例时，可先执行：

```bash
export SSL_CERT_FILE="$PWD/qwen-lan-ca.crt"
```

httpx 默认读取该环境变量，使用指定 CA 验证服务器；见 [httpx 证书环境变量说明](https://www.python-httpx.org/environment_variables/#ssl_cert_file)。

如果希望 Ubuntu 客户端的 curl 等系统工具默认信任这个 CA，可在该客户端安装公共证书：

```bash
sudo install -m 644 ./qwen-lan-ca.crt /usr/local/share/ca-certificates/qwen-lan-ca.crt
sudo update-ca-certificates
```

之后系统 curl 可以省略 `--cacert`；Python/httpx 继续使用上述 `SSL_CERT_FILE`，不依赖系统 CA 是否被 Python 的默认 CA 包采用。其他操作系统或浏览器按各自的信任存储导入 CA 公共证书。

在服务器确认服务的限制已生效：

```bash
systemctl show qwen-image-api nginx -p Id -p MemorySwapMax -p LimitCORE
```

`MemorySwapMax`、`LimitCORE` 应为 0。还需确认系统没有启用会采集进程内存或请求正文的转储、监控与休眠机制。上述配置不替代实际 HTTPS 链路的不落盘审计；验证步骤见[现有说明](testing.md#测试与写入审计)。

## 8. 日常启停与更新配置

如果 Nginx 专用于此 API，按需一起启动、停止或重启：

```bash
sudo systemctl start qwen-image-api nginx
sudo systemctl stop nginx qwen-image-api
sudo systemctl restart qwen-image-api nginx
sudo journalctl -u qwen-image-api -n 50 --no-pager
```

停止 Nginx 后，其私有内存挂载和运行目录会被清理。如果 Nginx 还有其他站点，只对 `qwen-image-api` 执行启停命令，不要连带停止共享的 Nginx。

修改 `/etc/qwen-image-api.env` 后重启 API 服务即可。修改端口时同步修改 Nginx 的 `proxy_pass`；增加排队或执行超时时，同步检查 `proxy_read_timeout` 和客户端超时。不要再额外启动 UI 或第二个 API 实例占用相同端口。

检查服务器证书有效期：

```bash
openssl x509 -in /etc/ssl/qwen/fullchain.pem -noout -dates
```

服务器证书到期前，复用已有 CA、服务器私钥、`server.csr` 和 `server.ext`，重新执行第 4.3 步的 `openssl x509 -req ...` 签发命令及后续验证，再按第 6 步在 Nginx 命名空间内检查配置，通过后执行 `sudo systemctl reload nginx`。不要重新生成 CA，客户端不需要重新安装同一 CA 的公共证书；CA 自身更换或到期时才需要更新客户端信任。服务器 IP 变化时先更新 `server.ext` 的 `subjectAltName` 并重新签发，同时修改 Nginx 监听地址、访问限制和客户端 URL。

更新至显式外部服务配置后，应先参照 [配置模板](../deploy/service.env.example) 补齐机器环境文件中的地址、模型和维度，
再重启 API。模型、维度和编码规则未变时可复用已有索引；变更后应重新构建。
描述工具使用的 `LLM_*` 只在手工运行工具时需要，不影响图像服务启动。

## 内存与隐私边界

`deploy/nginx.conf` 是 http 上下文中的配置示例；将域名、证书路径替换为实际值。部署前用 `ss -ltnp` 检查 443，若已有 Nginx 则合并配置，不替换其他站点。API 和 ComfyUI 不直接开放公网，关闭 8000/8188 的外部访问。TLS 证书需要预先提供；项目不申请证书或更改现有系统服务。

关闭请求和响应缓冲、磁盘缓存、响应临时文件及自动重试，代理上游使用 HTTP/1.1，特别覆盖 chunked 请求。Nginx 临时目录仍需作为防护边界配置到独立的、不可换出的 tmpfs，并创建 `body`/`proxy` 子目录，授予 Nginx 用户访问权限。不能仅认为 `/run` 或 `/dev/shm` 是 tmpfs 就断言不会写入 Swap。

`deploy/qwen-image-api.service` 使用当前路径和用户 `liu`；移机时修改三处路径及用户。真实环境文件建议放 `/etc/qwen-image-api.env`，权限 0600。审查配置后由管理员安装服务和 Nginx 配置，再执行 `nginx -t`、systemd 启动；这些系统修改未由源码安装自动执行。

严格隐私部署还需要管理员保证：

- 关闭磁盘 Swap 和休眠，或验证完整链路每个服务的 cgroup v2 `MemorySwapMax=0` 生效。示例仅配置 API/ComfyUI 单元；Nginx 需单独配置。不要在内存不足时直接执行 `swapoff -a`，应先评估并安排维护窗口。
- 禁用 Core Dump（单元 `LimitCORE=0`）、系统崩溃内存转储和相关诊断采集；检查 systemd-coredump、kdump、虚拟机快照及云主机能力。
- 禁止代理/WAF/APM/监控组件记录请求或响应正文、认证头及提示词；不注入 OpenTelemetry exporter 或第三方采集。FastAPI 原生 telemetry 在应用中显式关闭。
- 不启用会保存图片的额外 ComfyUI 插件、输出节点或外部缓存；模型与代码缓存允许持久化，不应删除模型目录。

存在磁盘 Swap 或尚未完成配置和审计时，不能声称严格零落盘。Python 对象释放和 CUDA 缓存回收也不构成 RAM/显存安全擦除；内核、驱动、宿主机和客户端均有独立边界。

部署配置必须覆盖 `/api/image/edit`、取消任务路由、`/api/pose/search`、`/health` 和 `/ready`，
见 [Nginx 模板](../deploy/nginx.conf)。只修改仓库模板不会更新 `/etc/nginx/conf.d/qwen-image-api.conf`；
合并路由后按[常用命令](service.md#nginx)检查并重载 Nginx，避免新接口被默认规则返回 404。

