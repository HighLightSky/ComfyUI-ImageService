import asyncio
import hmac
import os

import numpy as np
import torch
from aiohttp import web

from comfy_api.latest import ComfyExtension, io
from .image_service.config import Settings
from .image_service.errors import ServiceError
from .image_service.imaging import decode_rgb
from .image_service.memory import ImageMemory
from server import PromptServer


settings = None
memory = None


class MemoryImageInput(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="MemoryImageInput", category="image/memory", inputs=[io.String.Input("image_key")], outputs=[io.Image.Output()])

    @classmethod
    def execute(cls, image_key):
        if memory is None:
            raise RuntimeError("Start ComfyUI-ImageService/launch.py --api to use memory images")
        data = memory.take(image_key)
        with decode_rgb(data, settings) as image:
            array = np.array(image, dtype=np.float32)
        array /= 255.0
        return io.NodeOutput(torch.from_numpy(array).unsqueeze(0))


class MemoryImageRelease(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="MemoryImageRelease", category="image/memory", inputs=[io.String.Input("task_id")], outputs=[], is_output_node=True)

    @classmethod
    def execute(cls, task_id):
        if memory is None:
            raise RuntimeError("Memory API is not running")
        memory.release(task_id)
        return io.NodeOutput()


async def reap_resources(app):
    async def reap():
        while True:
            await asyncio.sleep(5)
            running, pending = PromptServer.instance.prompt_queue.get_current_queue_volatile()
            memory.reap({item[1] for item in running + pending})

    task = asyncio.create_task(reap())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for task_id in list(memory.tasks):
        memory.release(task_id)


def install_routes(server):
    @web.middleware
    async def internal_only(request, handler):
        path = request.path
        allowed = path in ("/ws", "/prompt", "/queue", "/history", "/internal-memory/status") or path.startswith(("/object_info/", "/internal-memory/tasks/")) or (path.startswith("/api/jobs/") and path.endswith("/cancel"))
        if not allowed:
            raise web.HTTPNotFound()
        token = request.headers.get("Authorization", "")
        if request.remote not in ("127.0.0.1", "::1") or not hmac.compare_digest(token.encode(), ("Bearer " + settings.internal_token).encode()):
            raise web.HTTPUnauthorized()
        submission_id = None
        try:
            if request.path == "/prompt" and request.method == "POST":
                body = await request.json()
                submission_id = body.get("prompt_id")
                if isinstance(submission_id, str):
                    with memory.lock:
                        memory.submitting.add(submission_id)
            return await handler(request)
        except ServiceError as error:
            return web.json_response({"error": {"code": error.code, "message": error.message}}, status=error.status)
        finally:
            if isinstance(submission_id, str):
                with memory.lock:
                    memory.submitting.discard(submission_id)

    server.app.middlewares.insert(0, internal_only)
    server.app.cleanup_ctx.append(reap_resources)

    @server.routes.get("/internal-memory/status")
    async def status(request):
        return web.json_response({"version": 1, **memory.stats()})

    @server.routes.post("/internal-memory/tasks/{task_id}")
    async def create(request):
        try:
            memory.create(request.match_info["task_id"])
        except ValueError:
            raise web.HTTPBadRequest() from None
        return web.json_response({"created": True}, status=201)

    @server.routes.get("/internal-memory/tasks/{task_id}")
    async def task_status(request):
        with memory.lock:
            submitting = request.match_info["task_id"] in memory.submitting
        return web.json_response({"submitting": submitting})

    @server.routes.post("/internal-memory/tasks/{task_id}/images")
    async def upload(request):
        task_id = request.match_info["task_id"]
        memory.begin_upload(task_id)
        data = bytearray()
        try:
            async with asyncio.timeout(settings.upload_timeout):
                async for chunk in request.content.iter_chunked(65536):
                    if len(data) + len(chunk) > settings.max_image_bytes:
                        raise ServiceError(413, "IMAGE_TOO_LARGE", "图片大小超限")
                    memory.reserve(task_id, len(chunk))
                    data.extend(chunk)
            key = memory.finish_upload(task_id, data)
        except BaseException:
            # Uploads happen before /prompt; no node can own this task yet.
            memory.release(task_id)
            raise
        return web.json_response({"image_key": key})

    @server.routes.delete("/internal-memory/tasks/{task_id}")
    async def release(request):
        task_id = request.match_info["task_id"]
        with memory.lock:
            if task_id in memory.submitting:
                raise web.HTTPConflict()
        running, pending = server.prompt_queue.get_current_queue_volatile()
        if any(item[1] == task_id for item in running + pending):
            raise web.HTTPConflict()
        memory.release(task_id)
        return web.json_response({"released": True})


class MemoryExtension(ComfyExtension):
    async def get_node_list(self):
        return [MemoryImageInput, MemoryImageRelease]


async def comfy_entrypoint():
    global settings, memory
    if os.environ.get("IMAGE_SERVICE_API_MODE") == "1":
        settings = Settings.from_env()
        memory = ImageMemory(settings)
        install_routes(PromptServer.instance)
    return MemoryExtension()
