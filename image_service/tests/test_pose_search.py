import asyncio
from dataclasses import replace
import hashlib
import io
import json
import shutil

from aiohttp import web
import httpx
import numpy as np
from PIL import Image
import pytest
import pytest_asyncio

from image_service.app import create_app
from image_service.config import EmbeddingSettings, Settings
from image_service.errors import ServiceError
from image_service.pose_search import EmbeddingClient, PoseIndex, PoseSearch
from image_service.tools import build_pose_index as builder


SETTINGS = Settings(api_token="a" * 40, internal_token="b" * 40)
MODEL = "Qwen3-Embedding-8B"
DIMENSIONS = 4096
QUERY_INSTRUCTION = "Retrieve human pose descriptions that best match the requested body posture, limb positions, and contact relationships."
DESCRIPTION = {"zh": "坐在地上，双手撑在身后。", "en": "Sitting on the floor with both hands supporting the body from behind."}


def vector(index=0, dimensions=DIMENSIONS):
    result = np.zeros(dimensions, dtype=np.float32)
    result[index] = 1
    return result


def metadata(ids=("b", "a", "c")):
    return {"version": 1, "model": MODEL, "dimensions": DIMENSIONS, "query_instruction": QUERY_INSTRUCTION, "records": [
        {"pose_id": name, "description": dict(DESCRIPTION), "pose": {"bones": {"head": [1, 2, 3]}, "camera": {"yaw_deg": 15}, "schema_version": 3}}
        for name in ids
    ]}


def write_index(path):
    builder.save_index(path, np.stack([vector(), vector(), vector(1)]), metadata())
    return path


class ComfyStub:
    def __init__(self):
        self.calls = 0

    async def start(self):
        pass

    async def close(self):
        pass

    async def ready(self):
        pass

    async def edit(self, task_id, images, prompt):
        self.calls += 1
        return bytes(images[0])


@pytest_asyncio.fixture
async def embedding_server(monkeypatch):
    state = {"requests": [], "responses": [], "block": False, "release": asyncio.Event(), "model": MODEL, "dimensions": DIMENSIONS}

    async def handle(request):
        payload = await request.json()
        state["requests"].append({"path": request.path, "authorization": request.headers.get("Authorization"), "payload": payload})
        if state["block"]:
            await state["release"].wait()
        if state["responses"]:
            status, body = state["responses"].pop(0)
            if isinstance(body, bytes):
                return web.Response(status=status, body=body)
            return web.json_response(body, status=status)
        return web.json_response({"model": state["model"], "data": [
            {"index": index, "embedding": vector(index % 2, state["dimensions"]).tolist()} for index in reversed(range(len(payload["input"])))
        ]})

    app = web.Application()
    app.router.add_post("/v1/embeddings", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/v1/embeddings"
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_URL", url)
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_API_KEY", "embedding-secret")
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_CA_FILE", "")
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_TIMEOUT", "1")
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_MODEL", MODEL)
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_DIMENSIONS", str(DIMENSIONS))
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_QUERY_INSTRUCTION", QUERY_INSTRUCTION)
    try:
        yield state
    finally:
        state["release"].set()
        await runner.cleanup()


def make_annotations(tmp_path, count=3):
    library = tmp_path / "library"
    library.mkdir()
    items = []
    for index in range(count):
        name = f"pose{index}"
        pose = {"bones": {"head": [index, 0, 0]}, "characters": [{"id": "character-1"}], "camera": {"yaw_deg": 15}, "schema_version": 3}
        pose_bytes = json.dumps(pose).encode()
        preview_bytes = b"preview"
        (library / f"{name}.json").write_bytes(pose_bytes)
        (library / f"{name}.webp").write_bytes(preview_bytes)
        fingerprint = hashlib.sha256(pose_bytes + b"\0" + f"{name}.webp".encode() + b"\0" + preview_bytes).hexdigest()
        items.append({"pose": f"{name}.json", "preview": f"{name}.webp", "source_sha256": fingerprint, "description": dict(DESCRIPTION)})
    annotations = tmp_path / "draft.json"
    annotations.write_text(json.dumps({"version": 1, "library": str(library), "items": items}))
    return annotations, library


def test_index_ranking_ties_and_full_json_snapshot(tmp_path):
    index = PoseIndex.load(write_index(tmp_path / "index.npz"))
    matches = index.search(vector(), 1000000)
    assert [match["pose_id"] for match in matches] == ["a", "b", "c"]
    assert [match["score"] for match in matches] == [1.0, 1.0, 0.0]
    assert matches[0]["pose"] == metadata()["records"][0]["pose"]
    assert len(index.search(vector(), 1)) == 1


def test_index_library_filter_before_top_k():
    data = metadata(("mixamo", "original-b", "original-a"))
    data["records"][0]["pose"]["_library"] = {"source": {"dataset": "Linzhan/Mixamo-Animations-Characters"}}
    index = PoseIndex(np.stack([vector(), vector(1), vector(1)]), data)
    assert [match["pose_id"] for match in index.search(vector(), 1)] == ["original-a"]
    assert [match["pose_id"] for match in index.search(vector(), 10, "default")] == ["original-a", "original-b"]
    assert [match["pose_id"] for match in index.search(vector(), 10, "mixamo")] == ["mixamo"]
    assert [match["pose_id"] for match in index.search(vector(), 10, "all")] == ["mixamo", "original-a", "original-b"]
    original = PoseIndex(np.stack([vector(), vector(), vector(1)]), metadata())
    assert original.search(vector(), 10, "mixamo") == []


@pytest.mark.asyncio
async def test_build_batches_order_fingerprints_and_independent_snapshot(tmp_path, embedding_server, monkeypatch):
    annotations, library = make_annotations(tmp_path)
    output = tmp_path / "index.npz"
    originals = {path: path.read_bytes() for path in library.iterdir()}
    monkeypatch.delenv("IMAGE_SERVICE_API_TOKEN", raising=False)
    await builder.build(annotations, output, 2)
    index = PoseIndex.load(output)
    assert np.array_equal(index.vectors, np.stack([vector(), vector(1), vector()]))
    requests = embedding_server["requests"]
    assert [len(request["payload"]["input"]) for request in requests] == [2, 1]
    assert requests[0]["payload"]["input"] == [DESCRIPTION["zh"]] * 2
    assert requests[0]["authorization"] == "Bearer embedding-secret"
    for record in index.records:
        assert len(record["description_sha256"]) == len(record["source_sha256"]) == 64
        assert record["pose"] == json.loads((library / record["pose_id"]).read_text())
    assert {path: path.read_bytes() for path in library.iterdir()} == originals
    assert "embedding-secret" not in json.dumps(index.metadata)
    shutil.rmtree(library)
    assert PoseIndex.load(output).search(vector(), 1)[0]["pose"]["schema_version"] == 3


@pytest.mark.parametrize("change", ["source", "preview", "description", "error", "escape", "duplicate"])
@pytest.mark.asyncio
async def test_build_rejects_bad_sources_before_requests_and_keeps_old_index(tmp_path, embedding_server, change):
    annotations, library = make_annotations(tmp_path)
    output = tmp_path / "index.npz"
    output.write_bytes(b"old index")
    draft = json.loads(annotations.read_text())
    if change == "source":
        (library / "pose0.json").write_text("{}")
    elif change == "preview":
        (library / "pose0.webp").unlink()
    elif change == "description":
        draft["items"][0]["description"]["zh"] = " "
    elif change == "error":
        draft["items"][0]["error"] = "review required"
    elif change == "escape":
        draft["items"][0]["pose"] = "../outside.json"
    else:
        draft["items"].append(dict(draft["items"][0]))
    annotations.write_text(json.dumps(draft))
    with pytest.raises((ValueError, OSError)):
        await builder.build(annotations, output, 2)
    assert output.read_bytes() == b"old index"
    assert not embedding_server["requests"]


@pytest.mark.asyncio
async def test_build_upstream_failure_does_not_publish_partial_index(tmp_path, embedding_server):
    annotations, _ = make_annotations(tmp_path)
    output = tmp_path / "index.npz"
    output.write_bytes(b"old index")
    embedding_server["responses"].extend([
        (200, {"model": MODEL, "data": [{"index": 0, "embedding": vector().tolist()}, {"index": 1, "embedding": vector().tolist()}]}),
        (503, {"error": "embedding-secret"}),
    ])
    with pytest.raises(ServiceError):
        await builder.build(annotations, output, 2)
    assert output.read_bytes() == b"old index"
    assert not list(tmp_path.glob(".index.npz*"))


def test_failed_atomic_publication_preserves_old_index(tmp_path, monkeypatch):
    output = tmp_path / "index.npz"
    output.write_bytes(b"old index")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(builder.np, "savez_compressed", fail)
    with pytest.raises(OSError):
        builder.save_index(output, np.stack([vector(), vector(), vector(1)]), metadata())
    assert output.read_bytes() == b"old index"
    assert not list(tmp_path.glob(".index.npz*"))


@pytest.mark.parametrize("bad", ["dimension", "zero", "nan", "duplicate", "model", "count", "json"])
@pytest.mark.asyncio
async def test_bad_embedding_responses_are_sanitized(embedding_server, bad):
    row = {"index": 0, "embedding": vector().tolist()}
    data = {"model": MODEL, "data": [row]}
    if bad == "dimension":
        row["embedding"] = [1.0]
    elif bad == "zero":
        row["embedding"] = [0.0] * DIMENSIONS
    elif bad == "nan":
        row["embedding"][0] = float("nan")
    elif bad == "duplicate":
        row["index"] = -1
    elif bad == "model":
        data["model"] = "wrong-model"
    elif bad == "count":
        data["data"] = []
    else:
        data = b"private upstream body embedding-secret"
    embedding_server["responses"].append((200, data))
    client = EmbeddingClient.from_env()
    await client.start()
    try:
        with pytest.raises(ServiceError) as raised:
            await client.embed(["private query"], MODEL, DIMENSIONS)
        assert raised.value.code == "EMBEDDING_PROTOCOL"
        assert "embedding-secret" not in str(raised.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_auth_validation_bounds_and_query_instruction(tmp_path, embedding_server):
    index = write_index(tmp_path / "index.npz")
    app = create_app(replace(SETTINGS, pose_index_path=str(index)), ComfyStub())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            assert (await http.post("/api/pose/search", json={"query": "坐姿"})).status_code == 401
            http.headers["Authorization"] = "Bearer " + SETTINGS.api_token
            for payload in ({}, [], {"query": " "}, {"query": 1}, {"query": "坐姿", "top_k": 0}, {"query": "坐姿", "top_k": True}, {"query": "坐姿", "top_k": 1.5}):
                response = await http.post("/api/pose/search", json=payload)
                assert response.status_code == 400
                assert response.json()["error"]["code"] == "INVALID_POSE_REQUEST"
                assert response.json()["request_id"]
            for library in ("unknown", "../mixamo", None, [], {}):
                response = await http.post("/api/pose/search", json={"query": "坐姿", "library": library})
                assert response.status_code == 400
                assert response.json()["error"]["code"] == "INVALID_POSE_REQUEST"
            for body in (b"not json", b" " * 65537):
                response = await http.post("/api/pose/search", content=body, headers={"Content-Type": "application/json"})
                assert response.status_code == 400
            assert (await http.post("/api/pose/search", content=b'{}')).status_code == 400
            assert not embedding_server["requests"]
            assert app.state.pose_search.active == 0
            response = await http.post("/api/pose/search", json={"query": "坐在地上", "model": "ignored"})
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert [match["pose_id"] for match in response.json()["matches"]] == ["a"]
            payload = embedding_server["requests"][0]["payload"]
            assert payload["model"] == MODEL
            assert payload["input"] == [f"Instruct: {QUERY_INSTRUCTION}\nQuery: 坐在地上"]
            response = await http.post("/api/pose/search", json={"query": "sitting", "top_k": 1000000})
            assert len(response.json()["matches"]) == 3


@pytest.mark.asyncio
async def test_api_library_selection(tmp_path, embedding_server):
    data = metadata(("original", "mixamo", "other-original"))
    data["records"][1]["pose"]["_library"] = {"source": {"dataset": "Linzhan/Mixamo-Animations-Characters"}}
    path = tmp_path / "index.npz"
    builder.save_index(path, np.stack([vector(1), vector(), vector(1)]), data)
    app = create_app(replace(SETTINGS, pose_index_path=str(path)), ComfyStub())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + SETTINGS.api_token}) as http:
            for options, expected in (({}, ["original"]), ({"library": "default"}, ["original"]), ({"library": "mixamo"}, ["mixamo"]), ({"library": "all"}, ["mixamo"])):
                response = await http.post("/api/pose/search", json={"query": "坐姿", **options})
                assert response.status_code == 200
                assert [match["pose_id"] for match in response.json()["matches"]] == expected
            response = await http.post("/api/pose/search", json={"query": "坐姿", "library": "default", "top_k": 100})
            assert [match["pose_id"] for match in response.json()["matches"]] == ["original", "other-original"]


@pytest.mark.parametrize("mode", ["disabled", "missing", "corrupt"])
@pytest.mark.asyncio
async def test_unavailable_index_does_not_break_image_readiness(tmp_path, mode):
    index = tmp_path / "index.npz"
    if mode == "corrupt":
        index.write_bytes(b"not a zip")
    settings = replace(SETTINGS, pose_index_path="" if mode == "disabled" else str(index))
    app = create_app(settings, ComfyStub())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + settings.api_token}) as http:
            assert (await http.get("/ready")).status_code == 200
            response = await http.post("/api/pose/search", json={"query": "坐姿"})
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "POSE_INDEX_NOT_READY"


@pytest.mark.parametrize("failure", ["upstream", "timeout"])
@pytest.mark.asyncio
async def test_api_upstream_errors_and_timeouts(tmp_path, embedding_server, failure):
    app = create_app(replace(SETTINGS, pose_index_path=str(write_index(tmp_path / "index.npz"))), ComfyStub())
    if failure == "upstream":
        embedding_server["responses"].append((401, {"error": "embedding-secret"}))
    else:
        embedding_server["block"] = True
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + SETTINGS.api_token}) as http:
            response = await http.post("/api/pose/search", json={"query": "private query"})
            assert response.status_code == (502 if failure == "upstream" else 504)
            assert "embedding-secret" not in response.text and "private query" not in response.text
            assert app.state.pose_search.active == 0


@pytest.mark.asyncio
async def test_search_concurrency_is_separate_from_image_queue(tmp_path, embedding_server):
    embedding_server["block"] = True
    backend = ComfyStub()
    app = create_app(replace(SETTINGS, pose_index_path=str(write_index(tmp_path / "index.npz"))), backend)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + SETTINGS.api_token}) as http:
            requests = [asyncio.create_task(http.post("/api/pose/search", json={"query": "坐姿"})) for _ in range(2)]
            try:
                async with asyncio.timeout(0.8):
                    while len(embedding_server["requests"]) < 2:
                        await asyncio.sleep(0.001)
                response = await http.post("/api/pose/search", json={"query": "第三个请求"})
                assert response.status_code == 429
                assert response.json()["error"]["code"] == "POSE_SEARCH_BUSY"
                assert app.state.gateway.admission.count == 0
                image = io.BytesIO()
                Image.new("RGB", (32, 32), "white").save(image, "PNG")
                response = await http.post("/api/image/edit", files={"images": ("a.png", image.getvalue())}, data={"prompt": "edit"})
                assert response.status_code == 200 and backend.calls == 1
            finally:
                embedding_server["release"].set()
                responses = await asyncio.gather(*requests)
            assert all(response.status_code == 200 for response in responses)
            assert app.state.pose_search.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("instruction", ["", "Find matching poses."])
async def test_configurable_model_build_and_query_without_auth(tmp_path, embedding_server, monkeypatch, instruction):
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_MODEL", "other-embedding")
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_DIMENSIONS", "8")
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_QUERY_INSTRUCTION", instruction)
    monkeypatch.delenv("IMAGE_SERVICE_POSE_EMBEDDING_API_KEY")
    embedding_server.update(model="other-embedding", dimensions=8)
    annotations, _ = make_annotations(tmp_path)
    output = tmp_path / "index.npz"
    await builder.build(annotations, output, 2)
    index = PoseIndex.load(output)
    assert index.vectors.shape == (3, 8)
    assert index.metadata["model"] == "other-embedding"
    assert index.metadata["query_instruction"] == instruction
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_QUERY_INSTRUCTION", "ignored at runtime")
    search = PoseSearch(str(output))
    await search.start()
    try:
        assert (await search.search("sitting", 1))[0]["pose_id"] == "pose0.json"
        request = embedding_server["requests"][-1]
        assert request["authorization"] is None
        assert request["payload"]["input"] == [f"Instruct: {instruction}\nQuery: sitting" if instruction else "sitting"]
        assert "dimensions" not in request["payload"]
    finally:
        await search.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("MODEL", "other"), ("DIMENSIONS", "8"), ("URL", ""), ("MODEL", ""), ("DIMENSIONS", "private invalid value")])
async def test_embedding_configuration_failure_is_isolated(tmp_path, embedding_server, monkeypatch, field, value):
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_" + field, value)
    app = create_app(replace(SETTINGS, pose_index_path=str(write_index(tmp_path / "index.npz"))), ComfyStub())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + SETTINGS.api_token}) as http:
            assert (await http.get("/ready")).status_code == 200
            response = await http.post("/api/pose/search", json={"query": "sitting"})
            assert response.status_code == 503
            assert "embedding-secret" not in response.text and "private invalid value" not in response.text
            if value in ("", "private invalid value"):
                assert "IMAGE_SERVICE_POSE_EMBEDDING_" + field in response.json()["error"]["message"]
            else:
                assert "重建索引" in response.json()["error"]["message"]
    assert not embedding_server["requests"]


@pytest.mark.parametrize("field,value", [("model", ""), ("model", 1), ("dimensions", True), ("dimensions", 0), ("dimensions", -1), ("dimensions", 8), ("query_instruction", None)])
def test_invalid_index_metadata(field, value):
    data = metadata()
    data[field] = value
    with pytest.raises(ValueError):
        PoseIndex(np.stack([vector(), vector(), vector(1)]), data)


@pytest.mark.parametrize("field,value", [("URL", "https://user:secret@example.com/v1/embeddings"), ("URL", "https://[secret"), ("DIMENSIONS", "0"), ("TIMEOUT", "0"), ("TIMEOUT", "secret")])
@pytest.mark.asyncio
async def test_invalid_embedding_config_names_variable_without_value(embedding_server, monkeypatch, field, value):
    monkeypatch.setenv("IMAGE_SERVICE_POSE_EMBEDDING_" + field, value)
    with pytest.raises(ValueError) as raised:
        EmbeddingSettings.from_env()
    assert "IMAGE_SERVICE_POSE_EMBEDDING_" + field in str(raised.value)
    assert "secret" not in str(raised.value)
