import subprocess
import sys
from types import SimpleNamespace

import pytest

from deploy import migrate_service as migration


@pytest.mark.parametrize("failure", [None, "image_service.tests.live_pose_check", "image_service.tests.check_deployment"])
def test_service_switch_preserves_credentials_and_rolls_back(tmp_path, monkeypatch, failure):
    plugin = tmp_path / "custom_nodes/ComfyUI-ImageService"
    (plugin / "deploy").mkdir(parents=True)
    (plugin / "deploy/qwen-image-api.service").write_text("new service\n")
    index = plugin / "image_service/pose_annotations/pose_index.npz"
    index.parent.mkdir(parents=True)
    index.touch()
    old_node = plugin.with_name("ComfyUI-MemoryImageIO")
    old_node.mkdir()
    disabled = old_node.with_name(old_node.name + ".disabled")
    unit = tmp_path / "qwen-image-api.service"
    unit.write_text("old service\n")
    env_file = tmp_path / "qwen-image-api.env"
    browser = tmp_path / "browser"
    browser.touch()
    certificate = tmp_path / "ca.crt"
    certificate.touch()
    original = f'IMAGE_SERVICE_API_TOKEN=existing-secret\nIMAGE_SERVICE_POSE_BROWSER_PATH={browser}\nIMAGE_SERVICE_POSE_INDEX_PATH=/old/index.npz\nIMAGE_SERVICE_POSE_EMBEDDING_QUERY_INSTRUCTION="a query instruction"\n'
    env_file.write_text(original)
    for name, value in (("PLUGIN_ROOT", plugin), ("COMFY_ROOT", tmp_path), ("UNIT", unit), ("ENV_FILE", env_file), ("OLD_NODE", old_node), ("DISABLED_NODE", disabled), ("BACKUP_ROOT", tmp_path / "backups")):
        monkeypatch.setattr(migration, name, value)
    monkeypatch.setattr(migration.os, "geteuid", lambda: 0)
    monkeypatch.setattr(migration.os, "getgrouplist", lambda *args: [1000])
    monkeypatch.setattr(migration.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_dir=str(tmp_path)))
    monkeypatch.setattr(migration.subprocess, "check_output", lambda *args, **kwargs: "liu\n")
    monkeypatch.setattr(sys, "argv", ["migrate_service.py", "--https-url", "https://example.test", "--ca-file", str(certificate)])
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert not any("existing-secret" in str(arg) for arg in command)
        if "-m" in command:
            assert kwargs["user"] == 1000
            assert kwargs["env"]["IMAGE_SERVICE_API_TOKEN"] == "existing-secret"
            assert kwargs["env"]["IMAGE_SERVICE_POSE_INDEX_PATH"] == str(index)
            assert kwargs["env"]["IMAGE_SERVICE_POSE_EMBEDDING_QUERY_INSTRUCTION"] == "a query instruction"
        if failure and failure in command:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(migration.subprocess, "run", run)
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            migration.main()
        assert old_node.is_dir() and not disabled.exists()
        assert unit.read_text() == "old service\n"
        assert env_file.read_text() == original
        assert calls[-1] == ["systemctl", "start", migration.SERVICE]
    else:
        migration.main()
        assert disabled.is_dir() and not old_node.exists()
        assert unit.read_text() == "new service\n"
        assert "IMAGE_SERVICE_API_TOKEN=existing-secret\n" in env_file.read_text()
        assert f"IMAGE_SERVICE_POSE_INDEX_PATH={index}\n" in env_file.read_text()
        assert ["systemctl", "restart", migration.SERVICE] in calls
    backup = next((tmp_path / "backups").iterdir())
    assert (backup / unit.name).read_text() == "old service\n"
    assert (backup / env_file.name).read_text() == original
