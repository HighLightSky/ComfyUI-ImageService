"""Default to ComfyUI UI; --api supervises the local image editing service."""

import os
import resource
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time

from image_service.config import Settings
from image_service.paths import COMFY_ROOT, PLUGIN_ROOT


def check_ports(settings):
    for port in (settings.api_port, settings.comfy_port):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(f"Port {port} is in use; stop the existing service before starting API mode") from None


def stop(children):
    deadline = time.monotonic() + 40
    for child in children:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def main():
    os.chdir(COMFY_ROOT)
    if "--api" not in sys.argv[1:]:
        os.execv(sys.executable, [sys.executable, str(COMFY_ROOT / "main.py"), *sys.argv[1:]])
    if sys.argv[1:] != ["--api"]:
        raise ValueError("API mode accepts only --api; use IMAGE_SERVICE_* environment variables for settings")

    os.environ.setdefault("IMAGE_SERVICE_INTERNAL_TOKEN", secrets.token_urlsafe(48))
    settings = Settings.from_env()
    check_ports(settings)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    environment = os.environ.copy()
    environment["IMAGE_SERVICE_API_MODE"] = "1"
    environment["IMAGE_SERVICE_SUPERVISED"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    environment["OTEL_SDK_DISABLED"] = "true"
    environment["ORT_DISABLE_TELEMETRY"] = "1"
    environment["TMPDIR"] = "/dev/shm"
    comfy = [
        sys.executable, "main.py", "--listen", "127.0.0.1", "--port", str(settings.comfy_port),
        "--cache-classic", "--disable-auto-launch", "--disable-assets", "--disable-metadata", "--offline",
        "--disable-all-custom-nodes", "--whitelist-custom-nodes", PLUGIN_ROOT.name, "websocket_image_save.py",
    ]
    gateway = [
        sys.executable, "-m", "uvicorn", "image_service.app:create_app", "--factory", "--app-dir", str(PLUGIN_ROOT), "--host", "127.0.0.1",
        "--port", str(settings.api_port), "--workers", "1", "--no-access-log", "--log-level", "critical",
        "--timeout-graceful-shutdown", str(settings.cleanup_timeout + 5),
    ]
    stopping = False

    def shutdown(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    with tempfile.TemporaryDirectory(prefix="qwen-image-api-", dir=environment["TMPDIR"]) as comfy_temp:
        comfy.extend(["--temp-directory", comfy_temp])
        # Child output can contain upstream exception payloads. Only supervisor state is logged.
        for attempt in range(3):
            children = []
            try:
                for command in (comfy, gateway):
                    children.append(subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                print(f"Image API starting on 127.0.0.1:{settings.api_port}; check /ready", flush=True)
                while not stopping and all(child.poll() is None for child in children):
                    time.sleep(0.5)
            finally:
                # Terminate the gateway first so its shutdown can cancel the current prompt.
                stop(list(reversed(children)))
            if stopping:
                return 0
            print("Image API child stopped; both processes released", flush=True)
            if attempt < 2:
                time.sleep(2)
                check_ports(settings)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
