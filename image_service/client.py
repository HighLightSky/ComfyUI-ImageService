import asyncio
import json
import struct
import uuid

import aiohttp

from .errors import ServiceError
from .imaging import run_image_operation, validate_png
from .workflow import MODEL_IDS, NODE_TYPES


class ComfyClient:
    def __init__(self, settings, workflow, fatal):
        self.settings = settings
        self.workflow = workflow
        self.fatal = fatal
        self.session = None

    async def start(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + self.settings.internal_token},
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(limit=8),
        )

    async def close(self):
        await self.session.close()

    async def request(self, method, path, **kwargs):
        try:
            async with self.session.request(method, self.settings.comfy_url + path, **kwargs) as response:
                if response.status >= 400:
                    code = "WORKFLOW_REJECTED" if path == "/prompt" and response.status == 400 else "COMFY_UNAVAILABLE"
                    raise ServiceError(422 if code == "WORKFLOW_REJECTED" else 502, code, "ComfyUI 拒绝任务或服务不可用")
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(data) + len(chunk) > 8 * 1024**2:
                        raise ServiceError(502, "COMFY_PROTOCOL", "ComfyUI 响应过大")
                    data.extend(chunk)
                result = json.loads(data) if data else None
                if result is not None and not isinstance(result, dict):
                    raise ServiceError(502, "COMFY_PROTOCOL", "ComfyUI 响应格式错误")
                return result
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise ServiceError(502, "COMFY_UNAVAILABLE", "ComfyUI 连接异常") from None

    async def ready(self):
        status = await self.request("GET", "/internal-memory/status")
        if status.get("version") != 1:
            raise ServiceError(502, "NOT_READY", "内存扩展不可用")
        for kind in (*NODE_TYPES.values(), "MemoryImageInput", "MemoryImageRelease"):
            info = await self.request("GET", "/object_info/" + kind)
            if kind not in info:
                raise ServiceError(502, "NOT_READY", "必要节点不可用")
            schema = info[kind]
            if kind == "MemoryImageInput" and schema.get("output") != ["IMAGE"]:
                raise ServiceError(502, "NOT_READY", "内存节点接口不匹配")
            if kind == "TextEncodeQwenImage21":
                required = schema.get("input", {}).get("required", {})
                if not {"prompt", "negative_prompt", "resolution", "clip"} <= required.keys():
                    raise ServiceError(502, "NOT_READY", "图像编码节点接口不匹配")
            for node_id in MODEL_IDS[:3]:
                node = self.workflow.template[node_id]
                if node["class_type"] == kind:
                    field = {"451": "unet_name", "453": "clip_name", "454": "vae_name"}[node_id]
                    options = schema["input"]["required"][field][0]
                    if node["inputs"][field] not in options:
                        raise ServiceError(502, "NOT_READY", "工作流模型文件不可用")

    async def run_graph(self, prompt_id, graph, want_image):
        client_id = str(uuid.uuid4())
        image = None
        current_node = None
        success = False
        try:
            async with self.session.ws_connect(
                self.settings.comfy_url + "/ws", params={"clientId": client_id},
                max_msg_size=self.settings.max_output_bytes + 8, heartbeat=30,
            ) as ws:
                submission = asyncio.create_task(self.request("POST", "/prompt", json={"prompt_id": prompt_id, "client_id": client_id, "prompt": graph}))
                try:
                    await asyncio.shield(submission)
                except asyncio.CancelledError:
                    await submission
                    raise
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        event = json.loads(message.data)
                        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                            raise ValueError("Invalid event")
                        data = event.get("data", {})
                        if data.get("prompt_id") != prompt_id:
                            continue
                        if event["type"] in ("execution_error", "execution_interrupted"):
                            raise ServiceError(502, "EXECUTION_FAILED", "ComfyUI 执行失败或已中断")
                        if event["type"] == "execution_success":
                            success = True
                        if event["type"] == "executing":
                            current_node = data.get("node")
                            if current_node is None:
                                if not success or (want_image and image is None):
                                    raise ServiceError(502, "MISSING_OUTPUT", "任务结束但没有有效输出")
                                return image
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        if not want_image or current_node != "503":
                            continue
                        if len(message.data) < 8 or struct.unpack(">II", message.data[:8]) != (1, 2):
                            raise ServiceError(502, "COMFY_PROTOCOL", "最终输出帧格式错误")
                        if image is not None:
                            raise ServiceError(502, "MULTIPLE_OUTPUTS", "任务返回了多个非预期输出")
                        image = message.data[8:]
                        await run_image_operation(validate_png, image, self.settings)
                    elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        break
        except (aiohttp.ClientError, ValueError, KeyError, TypeError):
            raise ServiceError(502, "COMFY_PROTOCOL", "ComfyUI WebSocket 连接或协议异常") from None
        raise ServiceError(502, "WEBSOCKET_CLOSED", "ComfyUI WebSocket 提前断开")

    async def ready_pose(self):
        try:
            for kind in ("LoraLoaderModelOnly", "EmptyLatentImage"):
                info = await self.request("GET", "/object_info/" + kind)
                if kind not in info:
                    raise ServiceError(503, "POSE_NOT_READY", "姿态工作流节点不可用")
                if kind == "LoraLoaderModelOnly":
                    options = info[kind]["input"]["required"]["lora_name"][0]
                    if self.workflow.pose_template["pose_lora"]["inputs"]["lora_name"] not in options:
                        raise ServiceError(503, "POSE_NOT_READY", "姿态 LoRA 不可用")
        except (ServiceError, KeyError, TypeError):
            raise ServiceError(503, "POSE_NOT_READY", "姿态工作流节点或 LoRA 未就绪") from None

    async def wait_idle(self, prompt_id):
        while True:
            status = await self.request("GET", f"/internal-memory/tasks/{prompt_id}")
            queue = await self.request("GET", "/queue")
            active = queue.get("queue_running", []) + queue.get("queue_pending", [])
            if not status["submitting"] and not any(item[1] == prompt_id for item in active):
                return
            await self.request("POST", f"/api/jobs/{prompt_id}/cancel")
            await asyncio.sleep(0.1)

    async def cleanup(self, task_id):
        cleanup_id = str(uuid.uuid4())
        try:
            async with asyncio.timeout(self.settings.cleanup_timeout):
                await self.request("POST", f"/api/jobs/{task_id}/cancel")
                await self.wait_idle(task_id)
                await self.run_graph(cleanup_id, self.workflow.cleanup(task_id), want_image=False)
                await self.request("DELETE", f"/internal-memory/tasks/{task_id}")
                await self.request("POST", "/history", json={"delete": [task_id, cleanup_id]})
        except Exception:
            self.fatal()
            raise ServiceError(502, "CLEANUP_FAILED", "任务清理失败，服务正在停止") from None

    async def finish_cleanup(self, task_id):
        task = asyncio.create_task(self.cleanup(task_id))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def edit(self, task_id, images, prompt):
        return await self._edit(task_id, images, prompt)

    async def edit_pose(self, task_id, images, prompt, width, height):
        await self.ready_pose()
        return await self._edit(task_id, images, prompt, (width, height))

    async def _edit(self, task_id, images, prompt, pose_size=None):
        try:
            await self.request("POST", f"/internal-memory/tasks/{task_id}")
            keys = []
            for data in images:
                result = await self.request("POST", f"/internal-memory/tasks/{task_id}/images", data=data, headers={"Content-Type": "application/octet-stream"})
                keys.append(result["image_key"])
            images.clear()
            async with asyncio.timeout(self.settings.execution_timeout):
                graph = self.workflow.build(keys, prompt) if pose_size is None else self.workflow.build_pose(keys, prompt, *pose_size)
                result = await self.run_graph(task_id, graph, want_image=True)
        except BaseException:
            try:
                await self.finish_cleanup(task_id)
            except ServiceError:
                pass
            raise
        await self.finish_cleanup(task_id)
        return result
