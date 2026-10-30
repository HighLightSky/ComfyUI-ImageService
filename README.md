<h1 align="center">ComfyUI 姿态引导图像生成 API</h1>

<p align="center">从姿势检索到图像生成，为应用与 Agent 提供统一的 API 调用入口。</p>

<p align="center">
  <a href="docs/installation.md"><img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.12+"></a>
  <a href="docs/installation.md"><img src="https://img.shields.io/badge/Platform-Linux-FCC624?style=flat-square&amp;logo=linux&amp;logoColor=black" alt="Platform: Linux"></a>
  <a href="docs/api.md"><img src="https://img.shields.io/badge/API-FastAPI-009688?style=flat-square&amp;logo=fastapi&amp;logoColor=white" alt="API: FastAPI"></a>
  <a href="docs/models.md"><img src="https://img.shields.io/badge/Model-Qwen%20Image%202.1-7C3AED?style=flat-square" alt="Model: Qwen Image 2.1"></a>
</p>

本项目是面向应用与 Agent 的 ComfyUI 插件，整合 Qwen Image 2.1 图片编辑、自然语言姿势检索和姿态引导生成。上传参考图并提供文字指令，即可通过 HTTP API 获取 PNG 结果。

- **图片编辑**：支持多张参考图，根据文字指令修改画面内容。
- **姿势检索**：用自然语言描述目标动作，从姿势库中检索候选并返回完整姿态 JSON。
- **姿态引导生成**：结合人物参考图与姿态 JSON，引导生成图像中的人物姿态。

图片输入输出通过内存传递。插件安装在 `custom_nodes`，无需修改 ComfyUI 核心代码；普通启动只注册节点，由专用启动器管理 API 服务。

当前启动器适用于 Linux、Python 3.12+ 和支持 Qwen Image 2.1 的 ComfyUI。配置、接口、姿势资源、部署与测试说明见[详细文档](docs/README.md)。

## 克隆项目与安装依赖

使用现有 ComfyUI 的 `.venv`。插件尚未配置远端仓库，请将下方 URL 替换为实际发布地址。

```bash
cd /path/to/ComfyUI
git clone '替换为插件仓库URL' custom_nodes/ComfyUI-ImageService
cd custom_nodes/ComfyUI-ImageService
uv pip install --python ../../.venv/bin/python -r requirements.txt
```

## 下载权重文件

默认图片编辑工作流需要以下三个文件，来源为 [Comfy-Org/Qwen-Image-2.1](https://huggingface.co/Comfy-Org/Qwen-Image-2.1)。命令在插件根目录执行，下载到 ComfyUI 的对应模型目录；已有完整文件可跳过。

```bash
mkdir -p ../../models/{diffusion_models,text_encoders,vae}

# UNet
curl -fL -C - -o ../../models/diffusion_models/qwen_image_2.1_int8_convrot.safetensors \
  https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors

# 文本编码器（W4A8）
curl -fL -C - -o ../../models/text_encoders/qwen3vl_8b_w4a8.safetensors \
  https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_w4a8.safetensors

# VAE
curl -fL -C - -o ../../models/vae/qwen_image_2.1_vae_bf16.safetensors \
  https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors
```

姿态编辑另需 LoRA、浏览器及 VNCCS 渲染资源；姿势检索另需索引和 Embedding 服务，见[权重与资源](docs/models.md)。

## 启动服务

在插件根目录输入至少 32 字符的 API Token，再启动前台服务。默认 API 地址为 `http://127.0.0.1:8000`，专用 ComfyUI 使用 `127.0.0.1:8188`；这两个端口须空闲。

```bash
read -r -s -p 'API Token（至少 32 字符）: ' IMAGE_SERVICE_API_TOKEN
export IMAGE_SERVICE_API_TOKEN
../../.venv/bin/python launch.py --api
```

已按[部署指南](docs/deployment.md)安装 systemd 服务时，使用以下命令，与前台启动二选一：

```bash
sudo systemctl start qwen-image-api.service
```

## 关闭服务

前台运行时按 `Ctrl+C`，会同时停止 API 和专用 ComfyUI。systemd 服务使用：

```bash
sudo systemctl stop qwen-image-api.service
```
