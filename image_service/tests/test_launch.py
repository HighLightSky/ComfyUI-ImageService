import os
import sys
from pathlib import Path

import pytest

import launch
from image_service.paths import PLUGIN_ROOT


@pytest.fixture(autouse=True)
def restore_working_directory(monkeypatch):
    monkeypatch.chdir(PLUGIN_ROOT)


def test_default_launcher_preserves_ui_arguments_without_requiring_api_token(monkeypatch):
    monkeypatch.delenv("IMAGE_SERVICE_API_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["launch.py", "--port", "8288", "--lowvram"])
    called = []

    def exec_ui(executable, arguments):
        called.append((executable, arguments))
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", exec_ui)
    with pytest.raises(SystemExit):
        launch.main()
    assert called == [(sys.executable, [sys.executable, str(launch.COMFY_ROOT / "main.py"), "--port", "8288", "--lowvram"])]


def test_api_launcher_rejects_ui_arguments(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["launch.py", "--api", "--listen", "0.0.0.0"])
    with pytest.raises(ValueError, match="accepts only"):
        launch.main()


def test_api_launcher_uses_owned_tmpfs_directory(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["launch.py", "--api"])
    monkeypatch.setenv("IMAGE_SERVICE_API_TOKEN", "a" * 40)
    monkeypatch.setenv("IMAGE_SERVICE_INTERNAL_TOKEN", "b" * 40)
    monkeypatch.setattr(launch, "check_ports", lambda settings: None)
    monkeypatch.setattr(launch.resource, "setrlimit", lambda *args: None)
    monkeypatch.setattr(launch.signal, "signal", lambda *args: None)
    monkeypatch.setattr(launch.time, "sleep", lambda *args: None)
    commands = []

    class Child:
        def __init__(self, command, **kwargs):
            commands.append(command)

        def poll(self):
            return 0

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(launch.subprocess, "Popen", Child)
    assert launch.main() == 1
    comfy = commands[0]
    directory = Path(comfy[comfy.index("--temp-directory") + 1])
    assert directory.parent == Path("/dev/shm")
    assert directory.name.startswith("qwen-image-api-")
    assert not directory.exists()

    assert "ComfyUI-MemoryImageIO" not in comfy
    assert comfy[comfy.index("--whitelist-custom-nodes") + 1] == PLUGIN_ROOT.name
    gateway = commands[1]
    assert Path(gateway[gateway.index("--app-dir") + 1]) == PLUGIN_ROOT
