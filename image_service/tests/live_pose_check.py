"""Opt-in GPU verification of pose edits; service payloads stay in memory."""

import argparse
import asyncio
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
from unittest.mock import patch
import uuid

import aiohttp
from PIL import Image

from image_service.client import ComfyClient
from image_service.config import Settings
from image_service.pose_render import PoseRenderer
from image_service.workflow import Workflow


from image_service.paths import COMFY_ROOT, PLUGIN_ROOT, POSE_LIBRARY


class ReferenceWorkflow:
    def __init__(self, width, height):
        source = json.loads((PLUGIN_PLUGIN_ROOT / "image_service/vendor/qwen21-pose-skill/workflows/VNCCS_PoseStudio_QI2.1.json").read_text())
        links = {link[0]: link for link in source["links"]}
        self.graph = {}
        for node in source["nodes"]:
            if node["id"] not in (476, 477, 478, 479, 480, 481, 483, 484, 501):
                continue
            inputs = dict(node.get("widgets_values_named", {}))
            inputs.pop("control_after_generate", None)
            for port in node.get("inputs", []):
                if port.get("link") is not None:
                    link = links[port["link"]]
                    inputs[port["name"]] = [str(link[1]), link[2]]
            self.graph[str(node["id"])] = {"class_type": node["type"], "inputs": inputs}
        self.graph["479"]["inputs"].update(width=width, height=height)
        self.graph["481"]["inputs"]["seed"] = 424242
        self.graph["503"] = {"class_type": "SaveImageWebsocket", "inputs": {"images": ["480", 0]}}

    def build(self, keys, prompt):
        graph = copy.deepcopy(self.graph)
        graph["484"]["inputs"]["prompt"] = "Draw character from image2\n" + prompt
        for index, key in enumerate(keys, 1):
            graph[f"memory_{index}"] = {"class_type": "MemoryImageInput", "inputs": {"image_key": key}}
            graph["484"]["inputs"][f"images.image_{index}"] = [f"memory_{index}", 0]
        return graph

    def cleanup(self, task_id):
        graph = {key: copy.deepcopy(self.graph[key]) for key in ("476", "477", "478", "483", "501")}
        graph["memory_release"] = {"class_type": "MemoryImageRelease", "inputs": {"task_id": task_id}}
        return graph


async def check(args, environment, process):
    url = "http://127.0.0.1:" + environment["IMAGE_SERVICE_API_PORT"]
    comfy_url = "http://127.0.0.1:" + environment["IMAGE_SERVICE_COMFY_PORT"]
    headers = {"Authorization": "Bearer " + environment["IMAGE_SERVICE_API_TOKEN"]}
    internal = {"Authorization": "Bearer " + environment["IMAGE_SERVICE_INTERNAL_TOKEN"]}
    reference = (PLUGIN_ROOT / "image_service/vendor/qwen21-pose-skill/references/src_standing.png").read_bytes()
    pose_path = POSE_LIBRARY / "local_user_poses/Uncategorized/Standing.json"
    pose = json.loads(pose_path.read_text())
    pose["characters"][0]["poses"][0]["bones"]["upperarm_l"][2] += 100
    canvas = io.BytesIO()
    Image.new("RGB", (args.width, args.height), "black").save(canvas, "PNG")
    prompt = "Draw the woman from image2 wearing the same white T-shirt, blue jeans and white sneakers in a sunlit garden. Keep her face and hair."
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=1300)) as http:
        async with asyncio.timeout(120):
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Launcher exited")
                try:
                    async with http.get(url + "/ready") as response:
                        if response.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(1)
        print("READY", flush=True)
        for name, use_pose in (("plain_before", False), ("pose_api", True), ("plain_after", False)):
            form = aiohttp.FormData()
            form.add_field("images", canvas.getvalue(), filename="canvas.png", content_type="image/png")
            form.add_field("images", reference, filename="character.png", content_type="image/png")
            form.add_field("prompt", prompt)
            if use_pose:
                form.add_field("pose", json.dumps(pose), content_type="application/json")
            started = time.monotonic()
            print("START " + name, flush=True)
            async with http.post(url + "/api/image/edit", data=form) as response:
                if response.status != 200:
                    error = await response.json()
                    raise RuntimeError(f"HTTP {response.status}, {error.get('error', {}).get('code')}")
                data = await response.read()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                assert image.size == (args.width, args.height)
            if args.evidence_dir:
                (args.evidence_dir / (name + ".png")).write_bytes(data)
            print(json.dumps({"case": name, "seconds": round(time.monotonic() - started, 2), "bytes": len(data), "size": [args.width, args.height]}), flush=True)
            async with http.get(comfy_url + "/internal-memory/status", headers=internal) as response:
                stats = await response.json()
                assert stats["tasks"] == stats["images"] == stats["reserved_bytes"] == 0
        if args.reference_workflow:
            settings = Settings(api_token=environment["IMAGE_SERVICE_API_TOKEN"], internal_token=environment["IMAGE_SERVICE_INTERNAL_TOKEN"], comfy_port=int(environment["IMAGE_SERVICE_COMFY_PORT"]), pose_browser_path=args.browser)
            renderer = PoseRenderer(settings)
            client = ComfyClient(settings, ReferenceWorkflow(args.width, args.height), lambda: None)
            await client.start()
            try:
                png = await renderer.render(pose, args.width, args.height)
                print("START reference_workflow", flush=True)
                started = time.monotonic()
                data = await client.edit(str(uuid.uuid4()), [png, reference], prompt)
                reference_data = data
                if args.evidence_dir:
                    (args.evidence_dir / "reference_workflow.png").write_bytes(data)
                print(json.dumps({"case": "reference_workflow", "seed": 424242, "seconds": round(time.monotonic() - started, 2), "sha256": hashlib.sha256(data).hexdigest()}), flush=True)
                variant = ComfyClient(settings, Workflow(), lambda: None)
                await variant.start()
                try:
                    print("START pose_fixed_seed", flush=True)
                    started = time.monotonic()
                    with patch("image_service.workflow.secrets.randbits", return_value=424242):
                        data = await variant.edit_pose(str(uuid.uuid4()), [png, reference], prompt, args.width, args.height)
                    assert data == reference_data, "Pose API graph differs from the supplied workflow at the same seed"
                    if args.evidence_dir:
                        (args.evidence_dir / "pose_fixed_seed.png").write_bytes(data)
                    print(json.dumps({"case": "pose_fixed_seed", "seed": 424242, "seconds": round(time.monotonic() - started, 2)}), flush=True)
                finally:
                    await variant.close()
            finally:
                await renderer.close()
                await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", required=True)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--audit-prefix", type=Path)
    parser.add_argument("--evidence-dir", type=Path, help="Optional client-side PNG evidence, preferably on tmpfs")
    parser.add_argument("--reference-workflow", action="store_true")
    args = parser.parse_args()
    if args.evidence_dir:
        args.evidence_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, IMAGE_SERVICE_API_TOKEN=secrets.token_urlsafe(48), IMAGE_SERVICE_INTERNAL_TOKEN=secrets.token_urlsafe(48), IMAGE_SERVICE_API_PORT="8100", IMAGE_SERVICE_COMFY_PORT="8288", IMAGE_SERVICE_POSE_BROWSER_PATH=args.browser, TMPDIR="/dev/shm", PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, str(PLUGIN_ROOT / "launch.py"), "--api"]
    if args.audit_prefix:
        command = ["strace", "-ff", "-yy", "-s", "0", "-e", "trace=openat,creat,write,writev,pwrite64,unlink,unlinkat,rename,mmap", "-o", str(args.audit_prefix), *command]
    process = subprocess.Popen(command, cwd=PLUGIN_ROOT, env=environment, start_new_session=True)
    os.environ["TMPDIR"] = "/dev/shm"
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
