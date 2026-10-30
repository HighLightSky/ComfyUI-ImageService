# Image Service 文档

[返回插件 README](../README.md)。以下文档中的命令均在插件根目录执行，复用 ComfyUI 的 `../../.venv`，不用进入 `docs/`。

| 文档 | 内容 |
|---|---|
| [权重与资源](models.md) | 必需权重的文件名、下载来源、存放目录与可选姿势资源 |
| [安装与依赖维护](installation.md) | ComfyUI 要求、可选依赖及依赖锁定 |
| [服务启停](service.md) | systemd、前台 API、普通 UI 与测试页面 |
| [环境变量配置](configuration.md) | Token、Embedding、浏览器、限制与常见错误 |
| [调用与测试页面](usage.md) | Agent、curl 和网页调用方式 |
| [API 接口](api.md) | 请求字段、响应、取消任务及错误码 |
| [姿势库与维护工具](poses.md) | 姿势描述、Mixamo 导入与索引构建 |
| [工作流与生命周期](workflow.md) | 工作流、队列、内存清理与姿态渲染 |
| [systemd 与 HTTPS 部署](deployment.md) | Ubuntu、Nginx、TLS 和内存边界 |
| [插件迁移与回退](migration.md) | 从旧根目录入口迁移和恢复旧服务 |
| [测试与写入审计](testing.md) | 单元测试、GPU 验证及文件写入审计 |
