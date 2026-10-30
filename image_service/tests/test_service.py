import asyncio
import io
import os
import struct
import tempfile
import threading
import uuid
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from PIL import Image

from image_service.app import Admission, ImageResponse, create_app
from image_service.client import ComfyClient
from image_service.config import Settings
from image_service.paths import COMFY_ROOT
from image_service.errors import ServiceError
from image_service.imaging import decode_rgb, run_image_operation, validate_png
from image_service.memory import ImageMemory
from image_service.upload import MultipartUpload
from image_service.workflow import MODEL_IDS, Workflow, convert


SETTINGS = Settings(api_token="a" * 40, internal_token="b" * 40)


def png(color="red", size=(32, 32)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


def multipart(images, prompt="edit", tail=True):
    parts = [b'--test\r\nContent-Disposition: form-data; name="prompt"\r\n\r\n' + prompt.encode() + b'\r\n']
    for image in images:
        parts.append(b'--test\r\nContent-Disposition: form-data; name="images"; filename="../ignored.png"\r\nContent-Type: image/png\r\n\r\n' + image + b'\r\n')
    return b"".join(parts) + (b"--test--\r\n" if tail else b"")


@pytest.mark.parametrize("count", [1, 3, 10, 13])
def test_streaming_multipart_keeps_order_and_discards_excess(count, monkeypatch):
    def disk_forbidden(*args, **kwargs):
        raise AssertionError("Upload parser attempted to use a temporary file")

    monkeypatch.setattr(tempfile, "TemporaryFile", disk_forbidden)
    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", disk_forbidden)
    images = [png((index, 0, 0)) for index in range(count)]
    reserved = []
    parser = MultipartUpload("multipart/form-data; boundary=test", SETTINGS, reserved.append)
    body = multipart(images)
    for start in range(0, len(body), 7):
        parser.feed(body[start:start + 7])
    upload = parser.finish()
    assert upload.images == images[:10]
    assert upload.ignored == max(0, count - 10)
    assert sum(reserved) == sum(map(len, images[:10])) + len(b"edit")


@pytest.mark.parametrize("body", [multipart([]), multipart([png()], tail=False), multipart([png()], prompt=" ")])
def test_invalid_multipart(body):
    parser = MultipartUpload("multipart/form-data; boundary=test", SETTINGS, lambda size: None)
    with pytest.raises(ServiceError):
        parser.feed(body)
        parser.finish()


def test_ignored_image_still_counts_towards_request_limit():
    images = [png()] * 10
    settings = replace(SETTINGS, max_request_bytes=len(multipart(images)) + 100)
    parser = MultipartUpload("multipart/form-data; boundary=test", settings, lambda size: None)
    with pytest.raises(ServiceError, match="请求大小"):
        parser.feed(multipart(images + [b"x" * 1000]))


def test_multi_megabyte_upload_does_not_spool(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disk upload")

    monkeypatch.setattr(tempfile, "TemporaryFile", forbidden)
    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", forbidden)
    image = os.urandom(2 * 1024**2)
    parser = MultipartUpload("multipart/form-data; boundary=test", SETTINGS, lambda size: None)
    body = multipart([image])
    for start in range(0, len(body), 65536):
        parser.feed(body[start:start + 65536])
    assert parser.finish().images == [image]


@pytest.mark.parametrize("count", [1, 3, 5, 10])
def test_workflow_slots_and_independent_copies(count):
    workflow = Workflow()
    first = workflow.build([f"key-{index}" for index in range(count)], "精确修改")
    second = workflow.build(["other"], "another")
    for index in range(1, count + 1):
        assert first["474"]["inputs"][f"images.image_{index}"] == [f"memory_{index}", 0]
    assert f"images.image_{count + 1}" not in first["474"]["inputs"]
    assert first["474"]["inputs"]["prompt"] == "精确修改"
    assert second["memory_1"]["inputs"]["image_key"] == "other"
    assert first["458"]["inputs"]["seed"] != second["458"]["inputs"]["seed"]
    assert "images.image_5_1" not in first["474"]["inputs"]
    assert workflow.template["474"]["inputs"]["prompt"] == ""
    cleanup = workflow.cleanup(str(uuid.uuid4()))
    assert set(cleanup) == {*MODEL_IDS, "memory_release"}
    assert all(cleanup[key] == first[key] for key in MODEL_IDS)
    assert first["458"]["inputs"]["latent_image"] == ["474", 2]


@pytest.mark.skipif(not (COMFY_ROOT / "user/default/workflows/qwen_image_2_1_image_edit.json").is_file(), reason="Original local UI workflow is not part of the repository")
def test_checked_in_template_matches_supplied_ui_workflow():
    assert convert(COMFY_ROOT / "user/default/workflows/qwen_image_2_1_image_edit.json") == Workflow().template


def test_memory_budget_consumption_isolation_and_reaping():
    memory = ImageMemory(SETTINGS)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    for task in (first, second):
        memory.create(task)
        memory.begin_upload(task)
        memory.reserve(task, len(png()))
    key_a = memory.finish_upload(first, png("red"))
    key_b = memory.finish_upload(second, png("blue"))
    assert key_a != key_b
    assert memory.take(key_a) == png("red")
    with pytest.raises(ServiceError):
        memory.take(key_a)
    memory.tasks[first].expires = 0
    memory.reap({first})
    assert first in memory.tasks
    memory.reap(set())
    assert first not in memory.tasks
    assert memory.take(key_b) == png("blue")
    memory.release(second)
    assert memory.stats() == {"tasks": 0, "images": 0, "reserved_bytes": 0}
    memory = ImageMemory(replace(SETTINGS, comfy_memory_bytes=10))
    memory.create(first)
    with pytest.raises(ServiceError):
        memory.reserve(first, 11)
    assert memory.used == 0


def test_rgba_exif_and_png_validation():
    output = io.BytesIO()
    Image.new("RGBA", (32, 64), (255, 0, 0, 0)).save(output, "PNG")
    with decode_rgb(output.getvalue(), SETTINGS) as image:
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (255, 0, 0)
    output = io.BytesIO()
    image = Image.new("RGB", (32, 64), "red")
    exif = Image.Exif()
    exif[274] = 6
    image.save(output, "JPEG", exif=exif)
    with decode_rgb(output.getvalue(), SETTINGS) as decoded:
        assert decoded.size == (64, 32)
    validate_png(png(), SETTINGS)
    with pytest.raises(ServiceError):
        validate_png(png()[:-12], SETTINGS)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.block = None
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def start(self):
        pass

    async def close(self):
        pass

    async def ready(self):
        pass

    async def edit(self, task_id, images, prompt):
        self.calls.append((task_id, list(images), prompt))
        self.entered.set()
        try:
            if self.block is not None:
                await self.block.wait()
            await asyncio.sleep(0.01)
            return bytes(images[0])
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.mark.asyncio
async def test_http_auth_invalid_images_and_truncation():
    backend = FakeClient()
    app = create_app(SETTINGS, backend)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            response = await http.post("/api/image/edit", content=b"not read")
            assert response.status_code == 401
            headers = {"Authorization": "Bearer " + SETTINGS.api_token}
            response = await http.post("/api/image/edit", headers=headers, files=[("images", ("a.png", b"invalid"))], data={"prompt": "edit"})
            assert response.status_code == 422
            images = [png((index, 0, 0)) for index in range(10)] + [b"ignored malformed image"]
            response = await http.post("/api/image/edit", headers=headers, files=[("images", ("a.png", data)) for data in images], data={"prompt": "精准修改"})
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/png"
            assert response.headers["cache-control"] == "no-store"
            assert response.content == images[0]
            assert backend.calls[0][1] == images[:10]
            assert backend.calls[0][2] == "精准修改"
            assert app.state.gateway.admission.count == app.state.gateway.admission.used == 0


@pytest.mark.asyncio
async def test_queue_is_bounded_and_results_do_not_cross_requests():
    backend = FakeClient()
    backend.block = asyncio.Event()
    app = create_app(replace(SETTINGS, queue_size=1), backend)
    headers = {"Authorization": "Bearer " + SETTINGS.api_token}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as http:
            async def send(color):
                return await http.post("/api/image/edit", files=[("images", ("a.png", png(color)))], data={"prompt": color})

            first = asyncio.create_task(send("red"))
            await backend.entered.wait()
            second = asyncio.create_task(send("blue"))
            while app.state.gateway.queue.empty():
                await asyncio.sleep(0)
            third = await send("green")
            assert third.status_code == 429
            backend.block.set()
            red, blue = await asyncio.gather(first, second)
            assert red.content == png("red")
            assert blue.content == png("blue")
            assert app.state.gateway.admission.count == 0


@pytest.mark.asyncio
async def test_response_lease_released_on_failed_socket_send():
    admission = Admission(SETTINGS)
    lease = admission.admit()
    lease.reserve(100)
    response = ImageResponse(png(), lease)

    async def broken_send(message):
        raise OSError("disconnected")

    with pytest.raises(OSError):
        await response({"type": "http"}, None, broken_send)
    assert admission.count == admission.used == 0
    assert response.body == b""


@pytest.mark.asyncio
async def test_png_response_is_exact_and_uses_bounded_send_chunks():
    admission = Admission(SETTINGS)
    lease = admission.admit()
    payload = os.urandom(200_000)
    lease.reserve(len(payload))
    response = ImageResponse(payload, lease)
    chunks = []

    async def send(message):
        if message["type"] == "http.response.body":
            assert admission.count == 1
            chunks.append(message["body"])

    await response({"type": "http"}, None, send)
    assert b"".join(chunks) == payload
    assert max(map(len, chunks)) <= 65536
    assert admission.count == admission.used == 0


@pytest.mark.asyncio
async def test_request_cancellation_reaches_worker_and_releases_admission():
    backend = FakeClient()
    backend.block = asyncio.Event()
    app = create_app(SETTINGS, backend)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            request = asyncio.create_task(http.post("/api/image/edit", headers={"Authorization": "Bearer " + SETTINGS.api_token}, files=[("images", ("a.png", png()))], data={"prompt": "edit"}))
            await backend.entered.wait()
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await asyncio.wait_for(backend.cancelled.wait(), 1)
            await asyncio.wait_for(app.state.gateway.queue.join(), 1)
            assert app.state.gateway.admission.count == app.state.gateway.admission.used == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_cancel_api_stops_only_target_job_and_service_recovers(queued):
    backend = FakeClient()
    backend.block = asyncio.Event()
    app = create_app(replace(SETTINGS, queue_size=1), backend)
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + SETTINGS.api_token}) as http:
            async def send(task_id, color):
                return await http.post("/api/image/edit", headers={"X-Task-ID": task_id}, files=[("images", ("a.png", png(color)))], data={"prompt": color})

            first = asyncio.create_task(send(first_id, "red"))
            await asyncio.wait_for(backend.entered.wait(), 1)
            second = asyncio.create_task(send(second_id, "blue"))
            while app.state.gateway.queue.empty():
                await asyncio.sleep(0)
            target_id = second_id if queued else first_id
            response = await http.post(f"/api/image/edit/{target_id}/cancel")
            assert response.status_code == 202
            assert response.json() == {"task_id": target_id, "status": "cancelling"}
            assert response.headers["cache-control"] == "no-store"
            cancelled, retained = (second, first) if queued else (first, second)
            result = await asyncio.wait_for(cancelled, 1)
            assert result.status_code == 409
            assert result.json()["error"]["code"] == "TASK_CANCELLED"
            assert backend.cancelled.is_set() == (not queued)
            assert target_id not in app.state.gateway.jobs
            assert app.state.gateway.admission.count == 1
            assert not retained.done()
            assert (await http.post(f"/api/image/edit/{target_id}/cancel")).status_code == 404
            backend.block.set()
            result = await asyncio.wait_for(retained, 1)
            assert result.status_code == 200
            assert result.content == png("red" if queued else "blue")
            assert [call[0] for call in backend.calls] == ([first_id] if queued else [first_id, second_id])
            assert (await send(str(uuid.uuid4()), "green")).status_code == 200
            await asyncio.wait_for(app.state.gateway.queue.join(), 1)
            assert not app.state.gateway.jobs
            assert app.state.gateway.admission.count == app.state.gateway.admission.used == 0


@pytest.mark.asyncio
async def test_cancel_api_auth_task_ids_and_duplicate_requests():
    backend = FakeClient()
    backend.block = asyncio.Event()
    app = create_app(SETTINGS, backend)
    task_id = str(uuid.uuid4())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            path = f"/api/image/edit/{task_id}/cancel"
            assert (await http.post(path)).status_code == 401
            http.headers["Authorization"] = "Bearer " + SETTINGS.api_token
            response = await http.post(path)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "TASK_NOT_FOUND"
            for path in ("/api/image/edit", "/api/image/edit/invalid/cancel"):
                response = await http.post(path, headers={"X-Task-ID": "invalid"})
                assert response.status_code == 400
                assert response.json()["error"]["code"] == "INVALID_TASK_ID"

            async def send(value):
                return await http.post("/api/image/edit", headers={"X-Task-ID": value}, files=[("images", ("a.png", png()))], data={"prompt": "edit"})

            first = asyncio.create_task(send(task_id))
            await asyncio.wait_for(backend.entered.wait(), 1)
            response = await send(task_id.upper())
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "TASK_ID_IN_USE"
            assert not backend.cancelled.is_set()
            assert app.state.gateway.admission.count == 1
            assert (await http.post(f"/api/image/edit/{uuid.uuid4()}/cancel")).status_code == 404
            assert not backend.cancelled.is_set()
            backend.block.set()
            assert (await asyncio.wait_for(first, 1)).status_code == 200
            await asyncio.wait_for(app.state.gateway.queue.join(), 1)
            assert not app.state.gateway.jobs
            assert app.state.gateway.admission.count == app.state.gateway.admission.used == 0


@pytest.mark.asyncio
async def test_queue_wait_timeout_does_not_submit_abandoned_job():
    backend = FakeClient()
    backend.block = asyncio.Event()
    app = create_app(replace(SETTINGS, queue_timeout=0.05), backend)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + SETTINGS.api_token}) as http:
            async def send():
                return await http.post("/api/image/edit", files=[("images", ("a.png", png()))], data={"prompt": "edit"})

            first = asyncio.create_task(send())
            await backend.entered.wait()
            second = await send()
            assert second.status_code == 504
            assert second.json()["error"]["code"] == "QUEUE_TIMEOUT"
            assert app.state.gateway.admission.count == 1
            assert app.state.gateway.queue.empty()
            backend.block.set()
            assert (await first).status_code == 200
            await app.state.gateway.queue.join()
            assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_queued_disconnect_frees_slot_without_waiting_for_gpu():
    backend = FakeClient()
    backend.block = asyncio.Event()
    app = create_app(replace(SETTINGS, queue_size=1), backend)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + SETTINGS.api_token}) as http:
            async def send():
                return await http.post("/api/image/edit", files=[("images", ("a.png", png()))], data={"prompt": "edit"})

            first = asyncio.create_task(send())
            await backend.entered.wait()
            for _ in range(3):
                queued = asyncio.create_task(send())
                while app.state.gateway.queue.empty():
                    await asyncio.sleep(0)
                queued.cancel()
                await asyncio.gather(queued, return_exceptions=True)
                assert app.state.gateway.queue.empty()
                assert app.state.gateway.admission.count == 1
            backend.block.set()
            assert (await first).status_code == 200
            assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_cleanup_failure_poison_service():
    fatal = []
    client = ComfyClient(SETTINGS, Workflow(), lambda: fatal.append(True))

    async def unavailable(*args, **kwargs):
        raise ServiceError(502, "COMFY_UNAVAILABLE", "unavailable")

    client.request = unavailable
    with pytest.raises(ServiceError, match="清理失败"):
        await client.cleanup(str(uuid.uuid4()))
    assert fatal == [True]


@pytest.mark.asyncio
async def test_decode_cancellation_waits_for_thread_to_release_image_references():
    entered = threading.Event()
    finish = threading.Event()

    def decode():
        entered.set()
        finish.wait(timeout=2)

    task = asyncio.create_task(run_image_operation(decode))
    while not entered.is_set():
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "missing", "corrupt", "double", "failure", "disconnect", "wrong_header", "timeout", "slow_submit"])
async def test_real_websocket_protocol_and_cleanup(mode):
    sockets = {}
    releases = []
    deleted = []
    frames = []
    submission_entered = asyncio.Event()

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets[request.query["clientId"]] = ws
        async for message in ws:
            pass
        return ws

    async def submit(request):
        body = await request.json()
        task_id = body["prompt_id"]
        ws = sockets[body["client_id"]]
        cleanup = "memory_release" in body["prompt"]
        if not cleanup:
            submission_entered.set()

        async def emit():
            if mode == "timeout" and not cleanup:
                return
            async def event(kind, **data):
                await ws.send_json({"type": kind, "data": {"prompt_id": task_id, **data}})

            await event("executing", node="memory_release" if cleanup else "458")
            if not cleanup:
                await ws.send_bytes(struct.pack(">II", 1, 1) + b"preview")
            await event("executing", node="memory_release" if cleanup else "503")
            if not cleanup:
                await ws.send_json({"type": "executing", "data": {"prompt_id": "another-job", "node": "458"}})
                if mode == "disconnect":
                    await ws.close()
                    return
                if mode == "failure":
                    await event("execution_error", current_inputs={"sensitive": "never expose"})
                    return
                if mode != "missing":
                    data = b"bad" if mode == "corrupt" else png()
                    await ws.send_bytes(struct.pack(">II", 1, 1 if mode == "wrong_header" else 2) + data)
                    if mode == "double":
                        await ws.send_bytes(struct.pack(">II", 1, 2) + data)
            else:
                releases.append(body["prompt"]["memory_release"]["inputs"]["task_id"])
            await event("execution_success")
            await event("executing", node=None)

        frames.append(asyncio.create_task(emit()))
        if mode == "slow_submit" and not cleanup:
            await asyncio.sleep(0.1)
        return web.json_response({"prompt_id": task_id})

    async def other(request):
        if request.path == "/queue":
            return web.json_response({"queue_running": [], "queue_pending": []})
        if request.method == "GET":
            return web.json_response({"submitting": False})
        if request.path == "/history":
            deleted.extend((await request.json())["delete"])
        return web.json_response({"image_key": "random-key"})

    server = web.Application()
    server.router.add_get("/ws", websocket)
    server.router.add_post("/prompt", submit)
    server.router.add_route("*", "/{rest:.*}", other)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    fatal = []
    client = ComfyClient(replace(SETTINGS, comfy_port=port, execution_timeout=0.05 if mode == "timeout" else 900), Workflow(), lambda: fatal.append(True))
    await client.start()
    task_id = str(uuid.uuid4())
    try:
        if mode == "ok":
            assert await client.edit(task_id, [png()], "edit") == png()
        elif mode == "timeout":
            with pytest.raises(TimeoutError):
                await client.edit(task_id, [png()], "edit")
        elif mode == "slow_submit":
            task = asyncio.create_task(client.edit(task_id, [png()], "edit"))
            await submission_entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ServiceError) as caught:
                await client.edit(task_id, [png()], "edit")
            assert "never expose" not in str(caught.value)
        assert releases == [task_id]
        assert task_id in deleted
        assert not fatal
    finally:
        await client.close()
        await asyncio.gather(*frames, return_exceptions=True)
        await runner.cleanup()
