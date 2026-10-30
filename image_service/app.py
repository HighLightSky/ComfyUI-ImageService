import asyncio
import hmac
import json
import logging
import os
import signal
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect

from .client import ComfyClient
from .config import Settings
from .errors import ServiceError
from .imaging import canvas_size, run_image_operation
from .pose_render import PoseRenderer
from .pose_search import PoseSearch
from .upload import receive_upload
from .workflow import Workflow


logger = logging.getLogger("image_service")


class ErrorBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception as error:
            if started:
                return
            disconnected = isinstance(error, ClientDisconnect)
            response = JSONResponse({
                "error": {"code": "CLIENT_DISCONNECTED" if disconnected else "INTERNAL_ERROR", "message": "客户端已断开" if disconnected else "服务内部错误"},
                "request_id": request_id,
            }, status_code=499 if disconnected else 500, headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)


class Admission:
    def __init__(self, settings):
        self.settings = settings
        self.count = 0
        self.used = 0

    def admit(self):
        if self.count >= self.settings.queue_size + 1:
            raise ServiceError(429, "QUEUE_FULL", "服务繁忙，请稍后重试")
        self.count += 1
        return Lease(self)


class Lease:
    def __init__(self, admission):
        self.admission = admission
        self.reserved = 0
        self.request_done = False
        self.worker_done = True
        self.closed = False

    def reserve(self, size):
        if self.admission.used + size > self.admission.settings.gateway_memory_bytes:
            raise ServiceError(429, "MEMORY_BUSY", "服务图像内存预算不足")
        self.admission.used += size
        self.reserved += size

    def finish(self, worker=False):
        if worker:
            self.worker_done = True
        else:
            self.request_done = True
        if self.request_done and self.worker_done and not self.closed:
            self.closed = True
            self.admission.used -= self.reserved
            self.admission.count -= 1


class ImageResponse(Response):
    def __init__(self, image, lease):
        super().__init__(image, media_type="image/png", headers={"Cache-Control": "no-store"})
        self.lease = lease

    async def __call__(self, scope, receive, send):
        try:
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            for start in range(0, len(self.body), 65536):
                await send({"type": "http.response.body", "body": self.body[start:start + 65536], "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            self.body = b""
            self.lease.finish()


@dataclass
class Job:
    task_id: str
    upload: object
    lease: Lease
    future: asyncio.Future
    deadline: float
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_error: ServiceError | None = None


class Gateway:
    def __init__(self, settings, client):
        self.settings = settings
        self.client = client
        self.admission = Admission(settings)
        self.queue = asyncio.Queue(maxsize=settings.queue_size + 1)
        self.jobs = {}
        self.poisoned = False
        self.pose_renderer = PoseRenderer(settings)

    def fatal(self):
        if not self.poisoned:
            self.poisoned = True
            if os.environ.get("IMAGE_SERVICE_SUPERVISED") == "1":
                asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)

    def cancel(self, job, error=None):
        if job.future.done() or job.cancel.is_set():
            return
        job.cancel_error = error or ServiceError(499, "CLIENT_DISCONNECTED", "客户端已断开")
        job.cancel.set()
        if job.started.is_set():
            return
        retained = []
        while not self.queue.empty():
            queued = self.queue.get_nowait()
            self.queue.task_done()
            if queued is job:
                queued.upload.images.clear()
                queued.upload.prompt = ""
                queued.upload.pose = None
                queued.started.set()
                queued.future.set_exception(job.cancel_error)
                queued.lease.finish(worker=True)
                self.jobs.pop(queued.task_id)
            else:
                retained.append(queued)
        for queued in retained:
            self.queue.put_nowait(queued)

    async def worker(self):
        while True:
            job = await self.queue.get()
            task = cancellation = None
            image = None
            outcome = "failed"
            started = time.monotonic()
            try:
                job.started.set()
                if job.cancel.is_set():
                    raise job.cancel_error
                if time.monotonic() > job.deadline:
                    raise ServiceError(504, "QUEUE_TIMEOUT", "排队等待超时")
                if self.poisoned:
                    raise ServiceError(502, "SERVICE_STOPPING", "服务正在停止")
                task = asyncio.create_task(self.generate(job))
                cancellation = asyncio.create_task(job.cancel.wait())
                await asyncio.wait((task, cancellation), return_when=asyncio.FIRST_COMPLETED)
                if job.cancel.is_set():
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise job.cancel_error
                image = await task
                job.lease.reserve(len(image))
                job.future.set_result(image)
                image = None
                outcome = "success"
            except TimeoutError:
                job.future.set_exception(ServiceError(504, "EXECUTION_TIMEOUT", "图像生成超时"))
            except ServiceError as error:
                if error.code == "CLEANUP_FAILED":
                    self.fatal()
                job.future.set_exception(error)
            except asyncio.CancelledError:
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if not job.future.done():
                    job.future.set_exception(ServiceError(502, "SERVICE_STOPPING", "服务正在停止"))
                raise
            except Exception:
                self.fatal()
                job.future.set_exception(ServiceError(500, "INTERNAL_ERROR", "服务内部错误"))
            finally:
                if cancellation is not None:
                    cancellation.cancel()
                    await asyncio.gather(cancellation, return_exceptions=True)
                logger.info("task=%s status=%s seconds=%.2f", job.task_id, outcome, time.monotonic() - started)
                job.upload.images.clear()
                job.upload.prompt = ""
                job.upload.pose = None
                job.lease.finish(worker=True)
                self.jobs.pop(job.task_id)
                self.queue.task_done()
                job = task = cancellation = image = None

    async def generate(self, job):
        upload = job.upload
        if upload.pose is None:
            return await self.client.edit(job.task_id, upload.images, upload.prompt)
        async with asyncio.timeout(self.settings.execution_timeout):
            width, height = await run_image_operation(canvas_size, upload.images[0], self.settings)
            image = await self.pose_renderer.render(upload.pose, width, height)
            job.lease.reserve(len(image))
            if len(upload.images) == 1:
                upload.images.append(upload.images[0])
            upload.images[0] = image
            upload.pose = None
            return await self.client.edit_pose(job.task_id, upload.images, upload.prompt, width, height)


def create_app(settings=None, client=None):
    if client is None and os.environ.get("IMAGE_SERVICE_SUPERVISED") != "1":
        raise RuntimeError("Start ComfyUI-ImageService/launch.py --api so cleanup failures can restart ComfyUI")
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        nonlocal client
        workflow = Workflow()
        gateway = Gateway(settings, client)
        client = client or ComfyClient(settings, workflow, gateway.fatal)
        gateway.client = client
        app.state.gateway = gateway
        await client.start()
        poses = PoseSearch(settings.pose_index_path)
        app.state.pose_search = poses
        await poses.start()
        worker = asyncio.create_task(gateway.worker())
        try:
            yield
        finally:
            gateway.poisoned = True
            worker.cancel()
            try:
                async with asyncio.timeout(settings.cleanup_timeout + 5):
                    await asyncio.gather(worker, return_exceptions=True)
            except TimeoutError:
                pass
            while not gateway.queue.empty():
                job = gateway.queue.get_nowait()
                job.upload.images.clear()
                job.upload.prompt = ""
                job.upload.pose = None
                job.started.set()
                job.future.set_exception(ServiceError(502, "SERVICE_STOPPING", "服务正在停止"))
                job.lease.finish(worker=True)
                gateway.jobs.pop(job.task_id)
                gateway.queue.task_done()
            await client.close()
            await poses.close()
            await gateway.pose_renderer.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
                  telemetry={"tracing": False, "metrics": False, "logs": False, "operation_spans": False, "auto_configure": False})

    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        return JSONResponse({"error": {"code": error.code, "message": error.message}, "request_id": request.state.request_id}, status_code=error.status, headers={"Cache-Control": "no-store"})

    app.add_middleware(ErrorBoundary)

    def authenticate(request):
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), ("Bearer " + settings.api_token).encode()):
            raise ServiceError(401, "UNAUTHORIZED", "认证失败")

    def parse_task_id(value):
        try:
            return str(uuid.UUID(value))
        except ValueError:
            raise ServiceError(400, "INVALID_TASK_ID", "任务 ID 必须是 UUID") from None

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request):
        authenticate(request)
        gateway = app.state.gateway
        try:
            if gateway.poisoned:
                raise ServiceError(503, "NOT_READY", "服务正在停止")
            await gateway.client.ready()
        except ServiceError:
            raise ServiceError(503, "NOT_READY", "ComfyUI、节点或模型尚未就绪") from None
        return {"status": "ready"}

    @app.post("/api/image/edit/{task_id}/cancel")
    async def cancel(task_id: str, request: Request):
        authenticate(request)
        task_id = parse_task_id(task_id)
        gateway = app.state.gateway
        job = gateway.jobs.get(task_id)
        if job is None or job.future.done():
            raise ServiceError(404, "TASK_NOT_FOUND", "任务不存在或已结束")
        gateway.cancel(job, ServiceError(409, "TASK_CANCELLED", "任务已取消"))
        return JSONResponse({"task_id": task_id, "status": "cancelling"}, status_code=202, headers={"Cache-Control": "no-store"})

    @app.post("/api/pose/search")
    async def pose_search(request: Request):
        authenticate(request)
        poses = app.state.pose_search
        with poses.admit():
            if request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ServiceError(400, "INVALID_POSE_REQUEST", "请求必须是 JSON，并包含非空 query 和正整数 top_k")
            body = bytearray()
            try:
                async with asyncio.timeout(settings.upload_timeout):
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > 65536:
                            raise ServiceError(400, "INVALID_POSE_REQUEST", "姿势检索请求不能超过 64 KiB")
                        body.extend(chunk)
            except TimeoutError:
                raise ServiceError(408, "UPLOAD_TIMEOUT", "接收姿势检索请求超时") from None
            try:
                data = json.loads(body)
            except (ValueError, UnicodeError):
                raise ServiceError(400, "INVALID_POSE_REQUEST", "姿势检索请求不是有效 JSON") from None
            if not isinstance(data, dict) or not isinstance(data.get("query"), str) or not data["query"].strip() or type(data.get("top_k", 1)) is not int or data.get("top_k", 1) < 1:
                raise ServiceError(400, "INVALID_POSE_REQUEST", "query 必须是非空字符串，top_k 必须是正整数")
            library = data.get("library", "default")
            if not isinstance(library, str) or library not in ("default", "mixamo", "all"):
                raise ServiceError(400, "INVALID_POSE_REQUEST", "library 必须是 default、mixamo 或 all")
            matches = await poses.search(data["query"], data.get("top_k", 1), library)
            return JSONResponse({"matches": matches}, headers={"Cache-Control": "no-store"})

    @app.post("/api/image/edit")
    async def edit(request: Request):
        authenticate(request)
        task_id = parse_task_id(request.headers.get("X-Task-ID", request.state.request_id))
        gateway = app.state.gateway
        if gateway.poisoned:
            raise ServiceError(502, "SERVICE_STOPPING", "服务正在停止")
        lease = gateway.admission.admit()
        handed_off = False
        job = None
        disconnect = waiter = None
        try:
            upload = await receive_upload(request, settings, lease.reserve)
            if task_id in gateway.jobs:
                raise ServiceError(409, "TASK_ID_IN_USE", "任务 ID 正在使用")
            future = asyncio.get_running_loop().create_future()
            future.add_done_callback(lambda value: value.exception() if not value.cancelled() else None)
            job = Job(task_id, upload, lease, future, time.monotonic() + settings.queue_timeout)
            gateway.queue.put_nowait(job)
            gateway.jobs[task_id] = job
            lease.worker_done = False

            async def watch_disconnect():
                while not await request.is_disconnected():
                    await asyncio.sleep(0.2)

            async def wait_result():
                try:
                    async with asyncio.timeout(settings.queue_timeout):
                        await job.started.wait()
                except TimeoutError:
                    raise ServiceError(504, "QUEUE_TIMEOUT", "排队等待超时") from None
                return await asyncio.shield(future)

            disconnect = asyncio.create_task(watch_disconnect())
            waiter = asyncio.create_task(wait_result())
            done, _ = await asyncio.wait((waiter, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                raise ServiceError(499, "CLIENT_DISCONNECTED", "客户端已断开")
            image = await waiter
            response = ImageResponse(image, lease)
            handed_off = True
            return response
        finally:
            for task in (disconnect, waiter):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (disconnect, waiter) if task is not None), return_exceptions=True)
            if not handed_off:
                if job is not None:
                    gateway.cancel(job)
                lease.finish()

    return app
