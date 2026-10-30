# 测试与写入审计

本文命令均在插件根目录执行，复用 ComfyUI 的 `../../.venv`。路径、用户名和服务地址按实际安装修改。

```bash
../../.venv/bin/python -m pytest --rootdir=.. image_service/tests -q

# 真实 GPU，临时启动 API 服务；输入、输出均为内存中的合成图片。
../../.venv/bin/python -m image_service.tests.live_check --counts 1 3 10 11

# 并发、真实任务中断、客户端断开及恢复。
../../.venv/bin/python -m image_service.tests.live_check --counts 1 --exercise-cancellation

# 在已验证不可换出的 tmpfs 上审计；跟踪文件名与写入长度，不记录数据正文。
../../.venv/bin/python -m image_service.tests.live_check \
  --counts 1 3 10 11 --audit-prefix /dev/shm/qwen-image-api-audit

# 超过 Starlette 常见临时文件阈值的实际输入，配合 I/O 审计。
../../.venv/bin/python -m image_service.tests.live_check --counts 1 --size 1024 \
  --noisy-input --audit-prefix /dev/shm/qwen-image-api-large-audit
```

审计使用 `strace -ff -yy -s 0` 追踪子进程的 open/write/pwrite/writev/unlink/mmap。逐项检查可写文件及共享可写映射，区分代码编译缓存与图片内容；同时观测 `input/`、`output/`、`temp/` 及代理临时目录。只对本次测试生成的审计文件进行管理，不清空 ComfyUI 持久化目录。`-s 0` 避免跟踪工具自身打印网络/文件数据正文。

测试依赖安装见[子项目依赖与维护入口](installation.md#子项目依赖与维护入口)。

完整公网验收还需在实际 TLS/Nginx 环境对大图、chunked 上传、并发、客户端断开、推理失败及超时进行相同审计。仅检查请求完成后没有图片文件不足以证明不落盘；应用测试也不能证明没有发生 Swap、内存快照或驱动转储。

姿态渲染与工作流的验证命令：

```bash
# 含实际浏览器的单元及隔离测试。
IMAGE_SERVICE_POSE_BROWSER_PATH=/absolute/path/to/chrome-headless-shell \
  ../../.venv/bin/python -m pytest --rootdir=.. image_service/tests -q

# 显式运行真实 GPU 验证，临时使用 8100/8288；PNG 默认仅留在内存。
../../.venv/bin/python -m image_service.tests.live_pose_check \
  --browser /absolute/path/to/chrome-headless-shell --reference-workflow
```

可追加 `--audit-prefix /dev/shm/pose-audit` 进行子进程文件写入审计；
`--evidence-dir` 是测试客户端显式保存结果的可选路径，不是服务端输出目录。
