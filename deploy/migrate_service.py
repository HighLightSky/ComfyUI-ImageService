"""Migrate this machine's existing systemd deployment, with GPU checks and rollback."""

import argparse
from datetime import datetime
import os
from pathlib import Path
import pwd
import shlex
import shutil
import subprocess


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
COMFY_ROOT = PLUGIN_ROOT.parents[1]
UNIT = Path("/etc/systemd/system/qwen-image-api.service")
ENV_FILE = Path("/etc/qwen-image-api.env")
SERVICE = "qwen-image-api.service"
BACKUP_ROOT = Path("/var/backups/qwen-image-api")
OLD_NODE = COMFY_ROOT / "custom_nodes/ComfyUI-MemoryImageIO"
DISABLED_NODE = OLD_NODE.with_name(OLD_NODE.name + ".disabled")


def systemctl(*arguments):
    return subprocess.run(["systemctl", *arguments], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--https-url", required=True, help="Existing HTTPS endpoint; Nginx is not modified")
    parser.add_argument("--ca-file", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run with sudo; the existing unit and environment file are root-owned")
    if not args.https_url.startswith("https://"):
        parser.error("--https-url must use HTTPS")
    if not OLD_NODE.is_dir() or DISABLED_NODE.exists():
        parser.error("Expected the original enabled memory node and no .disabled backup")
    username = subprocess.check_output(["systemctl", "show", SERVICE, "-p", "User", "--value"], text=True).strip()
    user = pwd.getpwnam(username)
    if user.pw_uid == 0:
        parser.error("The inference service must run as a regular user")
    environment = os.environ.copy()
    lines = ENV_FILE.read_text().splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = shlex.split(line, comments=True)
        if len(parts) != 1 or "=" not in parts[0]:
            raise ValueError(f"Unsupported environment syntax on line {number}")
        name, value = parts[0].split("=", 1)
        environment[name] = value
    index = PLUGIN_ROOT / "image_service/pose_annotations/pose_index.npz"
    if not index.is_file():
        parser.error("Copy and validate the pose index before switching")
    browser = environment.get("IMAGE_SERVICE_POSE_BROWSER_PATH", "")
    if not Path(browser).is_file() or not Path(args.ca_file).is_file():
        parser.error("The existing browser and CA certificate must be available")
    environment.update(HOME=user.pw_dir, USER=username, LOGNAME=username, PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1", TMPDIR="/dev/shm", IMAGE_SERVICE_POSE_INDEX_PATH=str(index))
    python = COMFY_ROOT / ".venv/bin/python"

    def verify(module, *arguments):
        subprocess.run([str(python), "-m", module, *arguments], cwd=PLUGIN_ROOT, env=environment, user=user.pw_uid, group=user.pw_gid, extra_groups=os.getgrouplist(username, user.pw_gid), check=True)

    backup = BACKUP_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(UNIT, backup / UNIT.name)
    shutil.copy2(ENV_FILE, backup / ENV_FILE.name)
    print(f"CONFIG_BACKUP={backup}", flush=True)
    node_disabled = False
    try:
        systemctl("stop", SERVICE)
        OLD_NODE.rename(DISABLED_NODE)
        node_disabled = True
        verify("image_service.tests.live_check", "--counts", "1", "3", "10")
        verify("image_service.tests.live_pose_check", "--browser", browser, "--width", "256", "--height", "384")
        prefix = "IMAGE_SERVICE_POSE_INDEX_PATH="
        updated = [prefix + str(index) if line.startswith(prefix) else line for line in lines]
        if not any(line.startswith(prefix) for line in lines):
            updated.append(prefix + str(index))
        ENV_FILE.write_text("\n".join(updated) + "\n")
        ENV_FILE.chmod(0o600)
        shutil.copyfile(PLUGIN_ROOT / "deploy/qwen-image-api.service", UNIT)
        UNIT.chmod(0o644)
        systemctl("daemon-reload")
        systemctl("start", SERVICE)
        verify("image_service.tests.check_deployment", "--url", "http://127.0.0.1:" + environment.get("IMAGE_SERVICE_API_PORT", "8000"), "--search")
        verify("image_service.tests.check_deployment", "--url", args.https_url, "--ca-file", args.ca_file, "--search", "--edit")
        systemctl("restart", SERVICE)
        verify("image_service.tests.check_deployment", "--url", args.https_url, "--ca-file", args.ca_file, "--edit")
        print("MIGRATION_OK: plugin service running; original source and data retained", flush=True)
    except BaseException:
        print("MIGRATION_FAILED: restoring original service configuration", flush=True)
        subprocess.run(["systemctl", "stop", SERVICE])
        if node_disabled:
            DISABLED_NODE.rename(OLD_NODE)
        shutil.copy2(backup / UNIT.name, UNIT)
        shutil.copy2(backup / ENV_FILE.name, ENV_FILE)
        systemctl("daemon-reload")
        systemctl("start", SERVICE)
        raise


if __name__ == "__main__":
    main()
