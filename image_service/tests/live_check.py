"""Opt-in GPU smoke test. Synthetic inputs and generated PNGs stay in memory."""

import argparse
import asyncio
import hashlib
import io
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
from PIL import Image


from image_service.paths import COMFY_ROOT, PLUGIN_ROOT, POSE_LIBRARY


def reference(index, size, noisy=False):
    output = io.BytesIO()
    image = Image.frombytes("RGB", (size, size), os.urandom(size * size * 3)) if noisy else Image.new("RGB", (size, size), (40 + index * 17, 90, 150))
    image.save(output, "PNG")
    return output.getvalue()


async def check(args, environment, process):
    headers = {"Authorization": "Bearer " + environment["IMAGE_SERVICE_API_TOKEN"]}
    url = "http://127.0.0.1:" + environment.get("IMAGE_SERVICE_API_PORT", "8000")
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=1300)) as http:
        deadline = time.monotonic() + 120
        while True:
            if process.poll() is not None:
                raise RuntimeError("Service launcher exited before readiness")
            try:
                async with http.get(url + "/ready") as response:
                    if response.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("Service did not become ready; child logs are intentionally suppressed")
            await asyncio.sleep(1)
        async with http.get(url + "/health", headers={"Authorization": ""}) as response:
            assert response.status == 200
        async with http.get(url + "/ready", headers={"Authorization": ""}) as response:
            assert response.status == 401
        internal_url = "http://127.0.0.1:" + environment.get("IMAGE_SERVICE_COMFY_PORT", "8188")
        async with http.get(internal_url + "/internal-memory/status", headers={"Authorization": ""}) as response:
            assert response.status == 401
        async with http.get(internal_url + "/") as response:
            assert response.status == 404
        print("READY_AND_AUTH_BOUNDARIES_OK", flush=True)
        for count in args.counts:
            form = aiohttp.FormData()
            form.add_field("prompt", "Change the background color to warm yellow while preserving the composition.")
            input_bytes = 0
            for index in range(count):
                data = reference(index % 10, args.size, args.noisy_input)
                input_bytes += len(data)
                form.add_field("images", data, filename="synthetic.png", content_type="image/png")
            started = time.monotonic()
            print(json.dumps({"starting_images": count, "input_bytes": input_bytes}), flush=True)
            async with http.post(url + "/api/image/edit", data=form) as response:
                if response.status != 200:
                    body = await response.json()
                    raise RuntimeError(f"GPU request failed: HTTP {response.status}, code={body.get('error', {}).get('code')}")
                data = await response.read()
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    assert image.format == "PNG" and image.size == (args.size, args.size)
                print(json.dumps({"images": count, "seconds": round(time.monotonic() - started, 2), "png_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}), flush=True)
                del data
            async with http.get(url + "/ready") as response:
                assert response.status == 200
        if args.exercise_cancellation:
            comfy_url = "http://127.0.0.1:" + environment.get("IMAGE_SERVICE_COMFY_PORT", "8188")
            internal = {"Authorization": "Bearer " + environment["IMAGE_SERVICE_INTERNAL_TOKEN"]}

            async def generate(size=256):
                form = aiohttp.FormData()
                form.add_field("prompt", "Change the background color to yellow.")
                form.add_field("images", reference(0, size), filename="synthetic.png", content_type="image/png")
                async with http.post(url + "/api/image/edit", data=form) as response:
                    body = await response.read()
                    if response.status == 200:
                        with Image.open(io.BytesIO(body)) as image:
                            image.load()
                    return response.status

            async def active_prompt():
                async with asyncio.timeout(20):
                    while True:
                        async with http.get(comfy_url + "/queue", headers=internal) as response:
                            queue = await response.json()
                        if queue["queue_running"]:
                            return queue["queue_running"][0][1]
                        await asyncio.sleep(0.05)

            async def wait_released():
                async with asyncio.timeout(45):
                    while True:
                        async with http.get(comfy_url + "/internal-memory/status", headers=internal) as response:
                            stats = await response.json()
                        if stats["tasks"] == stats["images"] == stats["reserved_bytes"] == 0:
                            return
                        await asyncio.sleep(0.1)

            results = await asyncio.gather(generate(), generate())
            assert results == [200, 200]
            print(json.dumps({"concurrent_http_statuses": results}), flush=True)
            interrupted = asyncio.create_task(generate(1024))
            task_id = await active_prompt()
            async with http.post(comfy_url + f"/api/jobs/{task_id}/cancel", headers=internal) as response:
                assert response.status == 200
            assert await interrupted == 502
            await wait_released()
            print("TARGETED_INTERRUPTION_CLEANED", flush=True)
            disconnected = asyncio.create_task(generate(1024))
            await active_prompt()
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            await wait_released()
            assert await generate() == 200
            print("CLIENT_DISCONNECT_CLEANED_AND_RECOVERED", flush=True)
        comfy_url = "http://127.0.0.1:" + environment.get("IMAGE_SERVICE_COMFY_PORT", "8188")
        internal = {"Authorization": "Bearer " + environment["IMAGE_SERVICE_INTERNAL_TOKEN"]}
        async with http.get(comfy_url + "/internal-memory/status", headers=internal) as response:
            stats = await response.json()
            assert stats["tasks"] == stats["images"] == stats["reserved_bytes"] == 0
            print(json.dumps({"memory_after_requests": stats}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--counts", nargs="+", type=int, default=[1, 3, 10])
    parser.add_argument("--size", type=int, default=256)
    parser.add_argument("--audit-prefix", type=Path, help="strace output prefix; must be on non-swapping tmpfs")
    parser.add_argument("--exercise-cancellation", action="store_true")
    parser.add_argument("--noisy-input", action="store_true", help="Upload poorly compressible PNGs to exercise multi-MiB uploads")
    args = parser.parse_args()
    environment = os.environ.copy()
    environment["IMAGE_SERVICE_API_TOKEN"] = secrets.token_urlsafe(48)
    environment["IMAGE_SERVICE_INTERNAL_TOKEN"] = secrets.token_urlsafe(48)
    command = [sys.executable, str(PLUGIN_ROOT / "launch.py"), "--api"]
    if args.audit_prefix:
        command = ["strace", "-ff", "-yy", "-s", "0", "-e", "trace=openat,creat,write,writev,pwrite64,unlink,unlinkat,rename,mmap", "-o", str(args.audit_prefix), *command]
    process = subprocess.Popen(command, cwd=PLUGIN_ROOT, env=environment, start_new_session=True)
    try:
        asyncio.run(check(args, environment, process))
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=50)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


if __name__ == "__main__":
    main()
