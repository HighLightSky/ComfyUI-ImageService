"""Build a read-only pose retrieval snapshot from reviewed descriptions."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import numpy as np

from ..config import EmbeddingSettings
from ..errors import ServiceError
from ..pose_search import EmbeddingClient, PoseIndex
from .describe_poses import DEFAULT_OUTPUT, library_file, load_draft, valid_description


def load_records(annotations, output):
    draft = load_draft(annotations)
    library = Path(draft["library"]).resolve()
    if output.resolve().is_relative_to(library) or output.resolve() == annotations.resolve():
        raise ValueError("索引输出不能覆盖描述文件或位于姿势库内")
    records = []
    ids = set()
    for item in draft["items"]:
        if item.get("error") or not valid_description(item.get("description")):
            raise ValueError(f"姿势描述未完成或存在错误：{item['pose']}")
        pose_path = library_file(library, item["pose"])
        pose_id = pose_path.relative_to(library).as_posix()
        if pose_id in ids or pose_path.suffix != ".json":
            raise ValueError("姿势路径重复或不是 JSON 文件")
        if not item.get("preview"):
            raise ValueError(f"缺少预览路径：{pose_id}")
        pose_bytes = pose_path.read_bytes()
        preview_bytes = library_file(library, item["preview"]).read_bytes()
        fingerprint = hashlib.sha256(pose_bytes + b"\0" + item["preview"].encode("utf-8") + b"\0" + preview_bytes).hexdigest()
        if fingerprint != item["source_sha256"]:
            raise ValueError(f"姿势源文件已变化，请重新审核描述：{pose_id}")
        pose = json.loads(pose_bytes)
        if not isinstance(pose, dict) or not any(key in pose for key in ("bones", "characters", "poses")) or pose.get("_library", {}).get("asset_type") == "animation":
            raise ValueError(f"不是静态姿势 JSON：{pose_id}")
        description = {lang: item["description"][lang] for lang in ("zh", "en")}
        records.append({
            "pose_id": pose_id,
            "description": description,
            "description_sha256": hashlib.sha256(json.dumps(description, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
            "source_sha256": fingerprint,
            "pose": pose,
        })
        ids.add(pose_id)
    if not records:
        raise ValueError("没有可构建索引的姿势描述")
    return sorted(records, key=lambda record: record["pose_id"])


def save_index(output, vectors, metadata):
    PoseIndex(vectors, metadata)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix="." + output.name, delete=False) as handle:
            temporary = Path(handle.name)
            np.savez_compressed(handle, vectors=vectors, metadata=np.asarray(json.dumps(metadata, ensure_ascii=False)))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def build(annotations, output, batch_size):
    records = load_records(annotations, output)
    settings = EmbeddingSettings.from_env()
    client = EmbeddingClient(settings)
    vectors = []
    await client.start()
    try:
        for start in range(0, len(records), batch_size):
            batch = records[start:start + batch_size]
            vectors.append(await client.embed([record["description"]["zh"] for record in batch], settings.model, settings.dimensions))
            print(f"已向量化：{start + len(batch)}/{len(records)}", flush=True)
    finally:
        await client.close()
    metadata = {"version": 1, "model": settings.model, "dimensions": settings.dimensions, "query_instruction": settings.query_instruction, "records": records}
    save_index(output, np.concatenate(vectors), metadata)
    print(f"已保存 {len(records)} 条姿势索引：{output}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_OUTPUT / "draft.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT / "pose_index.npz")
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size 必须大于零")
    try:
        asyncio.run(build(args.annotations, args.output, args.batch_size))
    except (OSError, ValueError, ServiceError) as error:
        print(f"索引构建失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
