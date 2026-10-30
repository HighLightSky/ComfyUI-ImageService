import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import ssl
import threading
import urllib.error

from PIL import Image
import pytest

from image_service.tools import describe_poses as captions


DESCRIPTION = {"zh": "站立，双臂自然垂在身侧。", "en": "Standing with both arms hanging at the sides."}


def completion(content=None):
    return {"choices": [{"message": {"content": json.dumps(DESCRIPTION, ensure_ascii=False) if content is None else content, "reasoning_content": "private reasoning"}}]}


@pytest.fixture
def model_server(monkeypatch):
    requests = []
    responses = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            status, response = responses.pop(0) if responses else (200, completion())
            body = json.dumps(response).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1/chat/completions")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "vision-test")
    try:
        yield requests, responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def make_pose(library, name="standing", preview=True, **data):
    library.mkdir(parents=True, exist_ok=True)
    path = library / (name + ".json")
    path.write_text(json.dumps({"bones": {}, **data}), encoding="utf-8")
    if preview:
        Image.new("RGB", (24, 32), "white").save(path.with_suffix(".webp"))
    return path


def invoke(library, output, *extra):
    return captions.main(["--library", str(library), "--output-dir", str(output), *extra])


def read_draft(output):
    return json.loads((output / "draft.json").read_text(encoding="utf-8"))


def test_request_review_and_readonly_sources(tmp_path, model_server):
    library, output = tmp_path / "library", tmp_path / "review"
    make_pose(library)
    originals = {path: path.read_bytes() for path in library.iterdir()}
    assert invoke(library, output) == 0
    requests, _ = model_server
    assert len(requests) == 1
    assert requests[0]["path"] == "/v1/chat/completions"
    assert requests[0]["authorization"] == "Bearer test-secret"
    payload = requests[0]["body"]
    assert {key: payload[key] for key in ("model", "stream", "temperature", "max_tokens")} == {
        "model": "vision-test", "stream": False, "temperature": 0.2, "max_tokens": 2048,
    }
    text, image = payload["messages"][0]["content"]
    assert text == {"type": "text", "text": captions.PROMPT}
    prefix, encoded = image["image_url"]["url"].split(",", 1)
    assert prefix == "data:image/png;base64"
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as decoded:
        assert decoded.format == "PNG" and decoded.size == (24, 32)
    draft = read_draft(output)
    assert draft["items"][0]["description"] == DESCRIPTION
    assert draft["prompt_version"] == captions.PROMPT_VERSION
    for path in output.iterdir():
        assert "test-secret" not in path.read_text(encoding="utf-8")
        assert "private reasoning" not in path.read_text(encoding="utf-8")
    report = (output / "review.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," in report
    assert "standing.json" in report and DESCRIPTION["zh"] in report
    assert {path: path.read_bytes() for path in library.iterdir()} == originals


def test_resume_preserves_edits_and_render_only_never_requests(tmp_path, model_server, monkeypatch):
    library, output = tmp_path / "library", tmp_path / "review"
    for name in ("a", "b", "c"):
        make_pose(library, name)
    assert invoke(library, output, "--limit", "1") == 0
    draft = read_draft(output)
    draft["items"][0]["description"]["zh"] = '<script>alert("edited")</script>'
    captions.save_draft(output / "draft.json", draft)
    assert invoke(library, output) == 0
    assert len(model_server[0]) == 3
    assert read_draft(output)["items"][0]["description"]["zh"] == '<script>alert("edited")</script>'
    before = (output / "draft.json").read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("render-only attempted a request")

    monkeypatch.setattr(captions.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(captions.ssl, "create_default_context", forbidden)
    monkeypatch.delenv("LLM_BASE_URL")
    monkeypatch.delenv("LLM_MODEL")
    assert captions.main(["--output-dir", str(output), "--render-only"]) == 0
    assert (output / "draft.json").read_bytes() == before
    report = (output / "review.html").read_text(encoding="utf-8")
    assert "<script>" not in report and "&lt;script&gt;" in report


@pytest.mark.parametrize("changed_file", ["json", "preview"])
def test_changed_sources_preserve_description_without_requests(tmp_path, model_server, changed_file):
    library, output = tmp_path / "library", tmp_path / "review"
    pose = make_pose(library)
    assert invoke(library, output) == 0
    original = read_draft(output)["items"][0]
    if changed_file == "json":
        pose.write_text('{"bones": {"head": [1, 2, 3]}}', encoding="utf-8")
    else:
        Image.new("RGB", (24, 32), "red").save(pose.with_suffix(".webp"))
    assert invoke(library, output) == 1
    assert len(model_server[0]) == 1
    changed = read_draft(output)["items"][0]
    assert changed["description"] == original["description"]
    assert changed["source_sha256"] == original["source_sha256"]
    assert "源文件已变化" in changed["error"]


@pytest.mark.parametrize("content", ["not json", '{"zh": "描述", "en": " "}', "[]", '{"zh": 1, "en": "text"}'])
def test_invalid_response_can_be_retried(tmp_path, model_server, content):
    library, output = tmp_path / "library", tmp_path / "review"
    make_pose(library)
    model_server[1].append((200, completion(content)))
    assert invoke(library, output) == 1
    assert "error" in read_draft(output)["items"][0]
    assert invoke(library, output) == 0
    assert "error" not in read_draft(output)["items"][0]
    assert len(model_server[0]) == 2


def test_fenced_json(tmp_path, model_server):
    library, output = tmp_path / "library", tmp_path / "review"
    make_pose(library)
    model_server[1].append((200, completion("```json\n" + json.dumps(DESCRIPTION) + "\n```")))
    assert invoke(library, output) == 0
    assert read_draft(output)["items"][0]["description"] == DESCRIPTION


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_service_errors_stop_batch_and_preserve_progress(tmp_path, model_server, status, capsys):
    library, output = tmp_path / "library", tmp_path / "review"
    for name in ("a", "b", "c"):
        make_pose(library, name)
    model_server[1].extend([(200, completion()), (status, {"error": "test-secret"})])
    assert invoke(library, output) == 1
    assert len(model_server[0]) == 2
    draft = read_draft(output)
    assert len(draft["items"]) == 2
    assert draft["items"][0]["description"] == DESCRIPTION
    assert str(status) in draft["items"][1]["error"]
    assert (output / "review.html").exists()
    captured = capsys.readouterr()
    assert "test-secret" not in captured.out + captured.err + json.dumps(draft)


@pytest.mark.parametrize("reason", [ConnectionRefusedError(), ssl.SSLCertVerificationError("private detail")])
def test_connection_and_tls_failure_stop_batch(tmp_path, monkeypatch, reason):
    library, output = tmp_path / "library", tmp_path / "review"
    for name in ("a", "b"):
        make_pose(library, name)
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(1)
        raise urllib.error.URLError(reason)

    monkeypatch.setattr(captions.urllib.request, "urlopen", unavailable)
    monkeypatch.setenv("LLM_BASE_URL", "https://vision.example.com/v1/chat/completions")
    monkeypatch.setenv("LLM_MODEL", "vision-test")
    assert invoke(library, output) == 1
    assert len(calls) == 1
    assert len(read_draft(output)["items"]) == 1
    assert "private detail" not in (output / "draft.json").read_text(encoding="utf-8")


def test_missing_and_corrupt_previews_continue_and_skip_nonposes(tmp_path, model_server):
    library, output = tmp_path / "library", tmp_path / "review"
    make_pose(library, "a", preview=False)
    corrupt = make_pose(library, "b")
    corrupt.with_suffix(".webp").write_bytes(b"not an image")
    make_pose(library, "c")
    make_pose(library, "animation", _library={"asset_type": "animation"})
    make_pose(library / "animations", "other")
    make_pose(library / ".repository_git_cache", "hidden")
    (library / "repositories.user.json").write_text("{}")
    assert invoke(library, output) == 1
    assert len(model_server[0]) == 1
    items = read_draft(output)["items"]
    assert len(items) == 3
    assert "缺少" in items[0]["error"] and "解码" in items[1]["error"]
    assert items[2]["description"] == DESCRIPTION


def test_atomic_checkpoint_survives_interruption(tmp_path, model_server, monkeypatch):
    library, output = tmp_path / "library", tmp_path / "review"
    for name in ("a", "b"):
        make_pose(library, name)
    original = captions.describe
    calls = []

    def interrupt(*args, **kwargs):
        if not calls:
            calls.append(1)
            return original(*args, **kwargs)
        assert read_draft(output)["items"][0]["description"] == DESCRIPTION
        raise KeyboardInterrupt

    monkeypatch.setattr(captions, "describe", interrupt)
    assert invoke(library, output) == 1
    assert read_draft(output)["items"][0]["description"] == DESCRIPTION
    assert not list(output.glob(".draft.json*"))
    monkeypatch.setattr(captions, "describe", original)
    assert invoke(library, output) == 0
    assert len(model_server[0]) == 2


def test_output_cannot_be_in_library(tmp_path):
    library = tmp_path / "library"
    pose = make_pose(library)
    original = pose.read_bytes()
    assert invoke(library, library / "review") == 1
    assert not (library / "review").exists()
    assert pose.read_bytes() == original


def test_model_change_requires_new_directory(tmp_path, model_server, monkeypatch):
    library, output = tmp_path / "library", tmp_path / "review"
    make_pose(library)
    assert invoke(library, output) == 0
    before = (output / "draft.json").read_bytes()
    monkeypatch.setenv("LLM_MODEL", "different-model")
    assert invoke(library, output) == 1
    assert (output / "draft.json").read_bytes() == before
    assert len(model_server[0]) == 1



def test_description_service_without_auth(tmp_path, model_server, monkeypatch):
    library, output = tmp_path / "library", tmp_path / "review"
    make_pose(library)
    monkeypatch.delenv("LLM_API_KEY")
    assert invoke(library, output) == 0
    assert model_server[0][0]["authorization"] is None


@pytest.mark.parametrize("name", ["LLM_BASE_URL", "LLM_MODEL"])
def test_missing_description_config_does_not_write_or_request(tmp_path, model_server, monkeypatch, capsys, name):
    library, output = tmp_path / "library", tmp_path / "review"
    make_pose(library)
    monkeypatch.delenv(name)
    assert invoke(library, output) == 1
    assert name in capsys.readouterr().err
    assert not model_server[0] and not output.exists()
