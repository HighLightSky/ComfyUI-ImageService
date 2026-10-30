# 来源与迁移

此目录是 Qwen 姿势 skill 的本地源码快照，后续由 ComfyUI-ImageService 插件仓库统一维护。

- 上游：https://github.com/Arekejoker/qwen21-nsfw-pose-skill
- 来源提交：`15e107bf903f8a909dba937214d2f1e448e549a2`
- 迁移日期：2026-10-28
- 原工作目录：`/home/liu/wzt/qwen21-pose-skill`，保留作备份。
- 复制范围：原仓库 Git 跟踪的 150 个文件；未复制 `.git`、生成输出或 Python 缓存。
- 本地新增：本说明及 `env.sh`；原始脚本、工作流、文档和素材保持原样。

这里没有嵌套 Git 仓库或 submodule。代码和随包静态资源由父仓库管理，
`outputs/` 仍由随包 `.gitignore` 忽略。下载缓存、模型权重、虚拟环境和
ComfyUI 中已安装的 VNCCS 插件及姿势资源沿用父仓库的忽略规则。

原创代码保留随包 Apache-2.0 `LICENSE`。第三方姿势、示例图和模型的权利
仍归各自作者，具体来源见原 `README.md` 的说明；不要将代码许可证等同于素材许可证。

维护入口及本机运行方法见 [姿势库与维护工具](../../../docs/poses.md)。
