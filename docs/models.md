# 权重与资源

## 普通图片编辑：必需的三个权重

默认 [图片编辑工作流](../image_service/workflow_api.json) 和 [姿态工作流](../image_service/workflow_pose_api.json) 使用下列文件。下载源为 [Comfy-Org/Qwen-Image-2.1](https://huggingface.co/Comfy-Org/Qwen-Image-2.1)，表中路径均相对 ComfyUI 根目录。

| 组件 | 文件名 | 下载大小（约） | 存放目录 |
|---|---|---|---|
| UNet | [qwen_image_2.1_int8_convrot.safetensors](https://huggingface.co/Comfy-Org/Qwen-Image-2.1/blob/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors) | 7.26 GB | `models/diffusion_models/` |
| 文本编码器 | [qwen3vl_8b_w4a8.safetensors](https://huggingface.co/Comfy-Org/Qwen-Image-2.1/blob/main/text_encoders/qwen3vl_8b_w4a8.safetensors) | 6.31 GB | `models/text_encoders/` |
| VAE | [qwen_image_2.1_vae_bf16.safetensors](https://huggingface.co/Comfy-Org/Qwen-Image-2.1/blob/main/vae/qwen_image_2.1_vae_bf16.safetensors) | 676 MB | `models/vae/` |

三个文件的下载命令见 [README](../README.md#下载权重文件)。下载后的默认布局为：

```text
ComfyUI/models/
├── diffusion_models/qwen_image_2.1_int8_convrot.safetensors
├── text_encoders/qwen3vl_8b_w4a8.safetensors
└── vae/qwen_image_2.1_vae_bf16.safetensors
```

文件名必须与默认工作流一致。文本编码器使用 W4A8 版本；本插件默认关闭提示词扩写，不需要下载 `qwen3.5_9b_qwen_image_2.1_pe_*` 权重。模型目录由 ComfyUI 管理，也可沿用其 `extra_model_paths.yaml` 配置。

## 姿态编辑：额外的 LoRA 与渲染资源

| 组件 | 文件名 | 下载大小（约） | 存放目录 |
|---|---|---|---|
| 姿势 LoRA | [VNCCS_QI2_PoseStudioV1.1.safetensors](https://huggingface.co/MIUProject/VNCCS_PoseStudio_QI2.1/blob/main/VNCCS_QI2_PoseStudioV1.1.safetensors) | 159 MB | `ComfyUI/models/loras/` |

在插件根目录执行以下命令；不使用姿态编辑时无需下载：

```bash
mkdir -p ../../models/loras
curl -fL -C - -o ../../models/loras/VNCCS_QI2_PoseStudioV1.1.safetensors \
  https://huggingface.co/MIUProject/VNCCS_PoseStudio_QI2.1/resolve/main/VNCCS_QI2_PoseStudioV1.1.safetensors
```

还需准备：

- 安装 `requirements-pose.txt` 中的 Playwright 和 Chromium，设置 `IMAGE_SERVICE_POSE_BROWSER_PATH`。
- 从 [ComfyUI_VNCCS_Utils](https://github.com/AHEKOT/ComfyUI_VNCCS_Utils) 准备本地 `web/` 渲染资源，默认使用 `ComfyUI/custom_nodes/ComfyUI_VNCCS_Utils/web/`；其他位置通过 `IMAGE_SERVICE_POSE_ASSET_ROOT` 指定。API 模式不需要启用该插件的节点。

安装命令见[安装与启动](installation.md)，浏览器和资源路径见[环境变量配置](configuration.md)。

## 姿势检索：索引与 Embedding 服务

检索读取本地 `pose_index.npz`，并将查询发送给显式配置的 Embedding 服务。它不要求把 Embedding 权重放进 ComfyUI 的模型目录；上游模型、维度必须与索引一致。本机索引使用 `Qwen3-Embedding-8B`、4096 维。

本地姿势库、描述和索引由 Git 忽略，不随 clone 分发；新安装需要自行迁移有效索引，或按[姿势库与维护工具](poses.md#构建姿势检索索引)构建。连接参数见[配置说明](configuration.md#配置后台服务)。

运行时不会自动下载模型、浏览器或索引；普通图片编辑不要求准备姿势检索和姿态编辑资源。
