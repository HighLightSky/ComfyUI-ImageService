"""Check an already running deployment without saving image payloads."""

import argparse
import asyncio
import io
import json
import os
import ssl
import time

import aiohttp
from PIL import Image

from image_service.paths import PLUGIN_ROOT
from image_service.pose_search import PoseIndex


async def check(args):
    token = os.environ["IMAGE_SERVICE_API_TOKEN"]
    context = ssl.create_default_context(cafile=args.ca_file)
    headers = {"Authorization": "Bearer " + token}
    async with aiohttp.ClientSession(headers=headers, connector=aiohttp.TCPConnector(ssl=context), timeout=aiohttp.ClientTimeout(total=1300)) as http:
        async with asyncio.timeout(120):
            while True:
                try:
                    async with http.get(args.url + "/ready") as response:
                        if response.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(1)
        async with http.get(args.url + "/health", headers={"Authorization": ""}) as response:
            assert response.status == 200
        async with http.get(args.url + "/ready", headers={"Authorization": ""}) as response:
            assert response.status == 401
        print("DEPLOYMENT_READY_AND_AUTH_OK", flush=True)
        if args.search:
            path = os.environ.get("IMAGE_SERVICE_POSE_INDEX_PATH") or PLUGIN_ROOT / "image_service/pose_annotations/pose_index.npz"
            index = PoseIndex.load(path)
            for library in ("default", "mixamo", "all"):
                async with http.post(args.url + "/api/pose/search", json={"query": "站立，双手举过头顶", "top_k": 2, "library": library}) as response:
                    assert response.status == 200, f"Pose search returned HTTP {response.status}"
                    matches = (await response.json())["matches"]
                expected = {index.records[i]["pose_id"] for i in index.library_indices[library]}
                assert matches and all(m["pose_id"] in expected and isinstance(m["pose"], dict) for m in matches)
                print(json.dumps({"search_library": library, "matches": len(matches)}), flush=True)
        if args.edit:
            image = io.BytesIO()
            Image.new("RGB", (256, 256), (40, 90, 150)).save(image, "PNG")
            form = aiohttp.FormData()
            form.add_field("prompt", "Change the background color to warm yellow.")
            form.add_field("images", image.getvalue(), filename="synthetic.png", content_type="image/png")
            started = time.monotonic()
            async with http.post(args.url + "/api/image/edit", data=form) as response:
                assert response.status == 200, f"Image edit returned HTTP {response.status}"
                data = await response.read()
            with Image.open(io.BytesIO(data)) as output:
                output.load()
                assert output.format == "PNG" and output.size == (256, 256)
            print(json.dumps({"deployment_edit_seconds": round(time.monotonic() - started, 2), "png_bytes": len(data)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--ca-file")
    parser.add_argument("--search", action="store_true")
    parser.add_argument("--edit", action="store_true")
    args = parser.parse_args()
    args.url = args.url.rstrip("/")
    asyncio.run(check(args))


if __name__ == "__main__":
    main()
