import asyncio
import copy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import uuid

import httpx
from PIL import Image
import pytest

from image_service.app import create_app
from image_service.client import ComfyClient
from image_service.config import Settings
from image_service.paths import POSE_LIBRARY
from image_service.errors import ServiceError
from image_service.imaging import canvas_size
from image_service.pose_render import ASSETS, PoseRenderer, validate_render
from image_service.upload import MultipartUpload
from image_service.workflow import MODEL_IDS, POSE_MODEL_IDS, Workflow


SETTINGS = Settings(api_token="a" * 40, internal_token="b" * 40)
POSE = {"bones": {"upperarm_l": [0, 0, 90]}}


def image_bytes(size=(64, 96), color="black", exif=None):
    output = io.BytesIO()
    image = Image.new("RGB", size, color)
    image.save(output, "JPEG" if exif else "PNG", **({"exif": exif} if exif else {}))
    return output.getvalue()


def body_with_pose(value, duplicate=False):
    body = b'--test\r\nContent-Disposition: form-data; name="images"; filename="a.png"\r\n\r\n' + image_bytes() + b'\r\n'
    body += b'--test\r\nContent-Disposition: form-data; name="prompt"\r\n\r\nedit\r\n'
    part = b'--test\r\nContent-Disposition: form-data; name="pose"\r\n\r\n' + value + b'\r\n'
    return body + part * (2 if duplicate else 1) + b'--test--\r\n'


def parse_pose(value, settings=SETTINGS, duplicate=False):
    reserved = []
    parser = MultipartUpload("multipart/form-data; boundary=test", settings, reserved.append)
    body = body_with_pose(value, duplicate)
    for offset in range(0, len(body), 17):
        parser.feed(body[offset:offset + 17])
    return parser.finish(), sum(reserved)


def test_pose_upload_and_budget():
    encoded = json.dumps(POSE).encode()
    upload, reserved = parse_pose(encoded)
    assert upload.pose == POSE and upload.prompt == "edit"
    assert reserved == len(encoded) + len(image_bytes()) + len(b"edit")


@pytest.mark.parametrize("value", [b"null", b"", b"[]", b"{}", b"{", b'{"bones":{"head":[NaN,0,0]}}'])
def test_invalid_pose(value):
    with pytest.raises(ServiceError) as raised:
        parse_pose(value)
    assert raised.value.status == 400 and raised.value.code == "INVALID_POSE"


def test_duplicate_and_oversized_pose():
    with pytest.raises(ServiceError) as raised:
        parse_pose(json.dumps(POSE).encode(), duplicate=True)
    assert raised.value.status == 400
    with pytest.raises(ServiceError) as raised:
        parse_pose(json.dumps(POSE).encode(), settings=replace(SETTINGS, max_pose_bytes=10))
    assert raised.value.status == 413 and raised.value.code == "POSE_TOO_LARGE"


def test_pose_graph_prompt_slots_and_both_model_caches():
    workflow = Workflow()
    before = copy.deepcopy(workflow.template)
    plain_cleanup = workflow.cleanup("plain")
    assert set(plain_cleanup) == {*MODEL_IDS, "memory_release"}
    graph = workflow.build_pose(["pose-key", "character-key", "background-key"], "Use image2 and image3.", 512, 768)
    assert graph["474"]["inputs"]["prompt"] == "Draw character from image2\nUse image2 and image3."
    assert graph["474"]["inputs"]["resolution"] == 1024
    assert graph["pose_lora"]["inputs"]["strength_model"] == 1
    assert graph["pose_cache"]["inputs"]["model"] == ["pose_lora", 0]
    assert graph["458"]["inputs"]["latent_image"] == ["pose_latent", 0]
    assert graph["pose_latent"]["inputs"] == {"width": 512, "height": 768, "batch_size": 1}
    for i, key in enumerate(("pose-key", "character-key", "background-key"), 1):
        assert graph[f"memory_{i}"]["inputs"]["image_key"] == key
    cleanup = workflow.cleanup("task")
    assert set(cleanup) == {*MODEL_IDS, *POSE_MODEL_IDS, "memory_release"}
    workflow.validate_links(cleanup)
    assert workflow.template == before
    assert workflow.build(["black", "character"], "original")["474"]["inputs"]["prompt"] == "original"


def test_canvas_size_orientation_and_alignment():
    assert canvas_size(image_bytes((519, 755)), SETTINGS) == (512, 768)
    exif = Image.Exif()
    exif[274] = 6
    assert canvas_size(image_bytes((519, 755), exif=exif), SETTINGS) == (768, 512)


@pytest.mark.parametrize("data", [b"invalid", image_bytes((64, 64))])
def test_bad_render_is_upstream_failure(data):
    with pytest.raises(ServiceError) as raised:
        validate_render(data, 64, 96, SETTINGS)
    assert raised.value.status == 502 and raised.value.code == "POSE_RENDER_FAILED"


class Backend:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = None

    async def start(self):
        pass

    async def close(self):
        pass

    async def ready(self):
        pass

    async def edit(self, task_id, images, prompt):
        self.calls.append(("plain", list(images), prompt))
        return bytes(images[0])

    async def edit_pose(self, task_id, images, prompt, width, height):
        self.calls.append(("pose", list(images), prompt, width, height))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return bytes(images[0])


class Renderer:
    def __init__(self):
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = None
        self.calls = []

    async def render(self, pose, width, height):
        self.calls.append(copy.deepcopy(pose))
        self.entered.set()
        try:
            if self.release is not None:
                await self.release.wait()
            return image_bytes((width, height), "white")
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def close(self):
        pass


def form(pose=True):
    fields = [("images", ("canvas.png", image_bytes())), ("images", ("character.png", image_bytes(color="blue")))]
    data = {"prompt": "Use image2 for the character."}
    if pose:
        data["pose"] = json.dumps(POSE)
    return {"files": fields, "data": data}


@pytest.mark.asyncio
async def test_api_pose_keeps_single_uploaded_character():
    backend, renderer = Backend(), Renderer()
    app = create_app(SETTINGS, backend)
    async with app.router.lifespan_context(app):
        app.state.gateway.pose_renderer = renderer
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + SETTINGS.api_token}) as http:
            request = form()
            request["files"] = request["files"][1:]
            response = await http.post("/api/image/edit", **request)
            assert response.status_code == 200
            assert backend.calls == [("pose", [image_bytes(color="white"), image_bytes(color="blue")], "Use image2 for the character.", 64, 96)]
            assert renderer.calls == [POSE]
            assert app.state.gateway.admission.used == app.state.gateway.admission.count == 0


@pytest.mark.asyncio
async def test_api_pose_replaces_only_canvas_and_plain_path_unchanged():
    backend, renderer = Backend(), Renderer()
    app = create_app(SETTINGS, backend)
    async with app.router.lifespan_context(app):
        app.state.gateway.pose_renderer = renderer
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + SETTINGS.api_token}) as http:
            response = await http.post("/api/image/edit", **form())
            assert response.status_code == 200
            call = backend.calls[0]
            assert call[0] == "pose" and call[2:] == ("Use image2 for the character.", 64, 96)
            assert call[1] == [image_bytes(color="white"), image_bytes(color="blue")]
            assert renderer.calls == [POSE]
            assert (await http.post("/api/image/edit", **form(False))).status_code == 200
            assert backend.calls[1][0] == "plain" and len(renderer.calls) == 1
            assert app.state.gateway.admission.used == app.state.gateway.admission.count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["render", "model", "queue"])
async def test_pose_cancellation_clears_state(stage):
    backend, renderer = Backend(), Renderer()
    if stage == "render":
        renderer.release = asyncio.Event()
    else:
        backend.release = asyncio.Event()
    app = create_app(SETTINGS, backend)
    async with app.router.lifespan_context(app):
        app.state.gateway.pose_renderer = renderer
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + SETTINGS.api_token}) as http:
            first_id = str(uuid.uuid4())
            first = asyncio.create_task(http.post("/api/image/edit", headers={"X-Task-ID": first_id}, **form()))
            await (renderer.entered.wait() if stage == "render" else backend.entered.wait())
            task_id = first_id
            target = first
            if stage == "queue":
                task_id = str(uuid.uuid4())
                target = asyncio.create_task(http.post("/api/image/edit", headers={"X-Task-ID": task_id}, **form()))
                async with asyncio.timeout(2):
                    while task_id not in app.state.gateway.jobs:
                        await asyncio.sleep(0.001)
            job = app.state.gateway.jobs[task_id]
            assert (await http.post(f"/api/image/edit/{task_id}/cancel")).status_code == 202
            assert (await target).status_code == 409
            assert job.upload.pose is None and not job.upload.images and job.upload.prompt == ""
            if stage == "render":
                assert renderer.cancelled.is_set() and not backend.calls
            if stage == "queue":
                assert len(renderer.calls) == 1
                backend.release.set()
                assert (await first).status_code == 200
            assert not app.state.gateway.jobs and app.state.gateway.admission.used == 0


@pytest.mark.asyncio
async def test_disabled_renderer_only_rejects_pose():
    backend = Backend()
    app = create_app(SETTINGS, backend)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + SETTINGS.api_token}) as http:
            response = await http.post("/api/image/edit", **form())
            assert response.status_code == 503 and response.json()["error"]["code"] == "POSE_NOT_READY"
            assert (await http.post("/api/image/edit", **form(False))).status_code == 200
            assert (await http.get("/ready")).status_code == 200


@pytest.mark.asyncio
async def test_custom_pose_assets_are_served_locally(tmp_path):
    asset = tmp_path / "pose_studio/core.js"
    asset.parent.mkdir()
    asset.write_bytes(b"custom local resource")
    renderer = PoseRenderer(replace(SETTINGS, pose_asset_root=str(tmp_path)))
    replies = []

    class Route:
        class request:
            url = "http://127.0.0.1/pose_studio/core.js"

        async def fulfill(self, **kwargs):
            replies.append(kwargs)

    await renderer.route(Route())
    assert replies == [{"content_type": ASSETS["/pose_studio/core.js"], "body": b"custom local resource"}]
    with pytest.raises(ServiceError) as raised:
        await renderer.render(POSE, 64, 96)
    assert raised.value.code == "POSE_NOT_READY"


@pytest.mark.asyncio
async def test_missing_pose_assets_reject_before_launch(tmp_path):
    binary = tmp_path / "browser"
    binary.touch()
    renderer = PoseRenderer(replace(SETTINGS, pose_browser_path=str(binary), pose_asset_root=str(tmp_path)))
    with pytest.raises(ServiceError) as raised:
        await renderer.render(POSE, 64, 96)
    assert raised.value.code == "POSE_NOT_READY" and renderer.driver is None


def test_pose_asset_root_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_SERVICE_API_TOKEN", "a" * 40)
    monkeypatch.setenv("IMAGE_SERVICE_INTERNAL_TOKEN", "b" * 40)
    monkeypatch.setenv("IMAGE_SERVICE_POSE_ASSET_ROOT", str(tmp_path))
    assert PoseRenderer(Settings.from_env()).asset_root == tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(502, "POSE_RENDER_FAILED"), (504, "POSE_RENDER_TIMEOUT")])
async def test_render_failure_releases_job_without_submitting_model(status, code):
    backend = Backend()
    app = create_app(SETTINGS, backend)

    async def fail(pose, width, height):
        raise ServiceError(status, code, "姿态渲染失败")

    async with app.router.lifespan_context(app):
        app.state.gateway.pose_renderer.render = fail
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + SETTINGS.api_token}) as http:
            response = await http.post("/api/image/edit", **form())
            assert response.status_code == status and response.json()["error"]["code"] == code
            assert not backend.calls and not app.state.gateway.jobs
            assert app.state.gateway.admission.used == app.state.gateway.admission.count == 0
            assert (await http.post("/api/image/edit", **form(False))).status_code == 200


@pytest.mark.asyncio
async def test_missing_lora_does_not_create_comfy_task(monkeypatch):
    client = ComfyClient(SETTINGS, Workflow(), lambda: None)
    calls = []

    async def request(method, path, **kwargs):
        calls.append(path)
        return {"LoraLoaderModelOnly": {"input": {"required": {"lora_name": [[]]}}}}

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(ServiceError) as raised:
        await client.edit_pose("task", [image_bytes()], "prompt", 64, 96)
    assert raised.value.code == "POSE_NOT_READY" and calls == ["/object_info/LoraLoaderModelOnly"]


@pytest.mark.asyncio
async def test_real_renderer_context_isolation_cancel_timeout_and_network(tmp_path, monkeypatch):
    binary = os.environ.get("IMAGE_SERVICE_POSE_BROWSER_PATH")
    if not binary:
        pytest.skip("Set IMAGE_SERVICE_POSE_BROWSER_PATH for real Chromium verification")
    monkeypatch.setenv("TMPDIR", "/dev/shm")
    renderer = PoseRenderer(replace(SETTINGS, pose_browser_path=binary))
    pose = json.loads((POSE_LIBRARY / "local_user_poses/Uncategorized/Standing.json").read_text())
    original = copy.deepcopy(pose)
    requests = []
    serve = renderer.route

    async def track(route):
        requests.append(route.request.url)
        await serve(route)

    monkeypatch.setattr(renderer, "route", track)
    try:
        first = await renderer.render(pose, 256, 384)
        assert not renderer.browser.contexts
        character = next(c for c in pose["characters"] if c["id"] == pose["active_character_id"])
        character["poses"][pose["activeTab"]]["bones"]["upperarm_l"][2] += 90
        changed = await renderer.render(pose, 256, 384)
        assert first != changed and first == await renderer.render(original, 256, 384)
        camera_pose = copy.deepcopy(original)
        camera_pose["characters"][0]["poses"][0]["cameraParams"]["yaw_deg"] += 45
        assert first != await renderer.render(camera_pose, 256, 384)
        for size in ((256, 256), (384, 256)):
            with Image.open(io.BytesIO(await renderer.render(original, *size))) as image:
                assert image.size == size
        assert not renderer.browser.contexts
        assert all(url.startswith("http://127.0.0.1/") for url in requests)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked(route):
            entered.set()
            await release.wait()

        monkeypatch.setattr(renderer, "route", blocked)
        task = asyncio.create_task(renderer.render(original, 256, 384))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not renderer.browser.contexts
        release.set()
        await asyncio.sleep(0)
        release.clear()
        renderer.timeout = 0.05
        with pytest.raises(ServiceError) as raised:
            await renderer.render(original, 256, 384)
        assert raised.value.code == "POSE_RENDER_TIMEOUT" and not renderer.browser.contexts
        release.set()
        monkeypatch.setattr(renderer, "route", track)
        renderer.timeout = 30
        await renderer.browser.close()
        assert await renderer.render(original, 256, 384) == first
    finally:
        await renderer.close()
    assert renderer.browser is renderer.driver is None
