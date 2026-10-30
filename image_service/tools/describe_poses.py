"""Generate reviewable bilingual pose descriptions without changing the library."""

import argparse
import base64
import hashlib
import html
import http.client
import io
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import urllib.error
import urllib.request

from PIL import Image, ImageOps

from ..config import DescriptionSettings
from ..paths import PLUGIN_ROOT, POSE_LIBRARY


DEFAULT_LIBRARY = POSE_LIBRARY
DEFAULT_OUTPUT = PLUGIN_ROOT / "image_service/pose_annotations"
PROMPT_VERSION = 1
PROMPT = """你为人物姿势检索库编写图像描述。请仅依据图片实际可见的内容，分别用中文和英文简短描述人物姿态。

先概括整体姿势或动作，再描述有辨识度的躯干倾斜、四肢位置、头部方向和身体接触关系。人物左右以人物自身为准；无法确认时省略左右，不要猜测。可以使用常见动作名称，但不要推测静态图片无法证明的运动过程。

区分人物身体朝向和相机观察视角。只有视角清晰时才描述。局部特写只描述可见部位，不补全画面外或被遮挡的肢体。多人图片描述各人物的姿势与相对关系。

不描述人物身份、年龄、性别、容貌、服装、画风、背景、情绪或故事。不要臆测图片中不存在的座椅、道具、支撑物或接触关系。忽略文字、水印和界面元素。

中文通常为 40–100 字，英文通常为 30–70 词；简单姿势可以更短。两种语言语义一致，不堆砌同义词，不输出标签、建议或评价。

只返回 JSON：{"zh": "中文描述", "en": "English description"}，不要输出解释或 Markdown。"""
PREVIEW_SUFFIXES = (".webp", ".jpg", ".jpeg", ".png")


class ServiceUnavailable(Exception):
    pass


def library_file(library, relative):
    path = (library / relative).resolve()
    if not path.is_relative_to(library):
        raise ValueError("文件路径超出姿势库目录")
    return path


def scan_poses(library):
    poses = []
    for folder, dirs, files in os.walk(library):
        dirs[:] = sorted(name for name in dirs if not name.startswith(".") and name != "animations")
        for name in sorted(files):
            if not name.endswith(".json") or name in {"repositories.user.json", "pose_library.json"}:
                continue
            pose = library_file(library, Path(folder, name).relative_to(library))
            data = json.loads(pose.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict) or not any(key in data for key in ("bones", "characters", "poses")):
                continue
            if data.get("_library", {}).get("asset_type") == "animation":
                continue
            preview = next((pose.with_suffix(suffix) for suffix in PREVIEW_SUFFIXES if pose.with_suffix(suffix).is_file()), None)
            if preview is not None:
                preview = library_file(library, preview.relative_to(library))
            poses.append((pose, preview))
    return sorted(poses, key=lambda pair: pair[0].relative_to(library).as_posix())


def png_bytes(data, thumbnail=False):
    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image)
        if thumbnail:
            image.thumbnail((320, 320))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


def valid_description(value):
    return isinstance(value, dict) and all(isinstance(value.get(lang), str) and value[lang].strip() for lang in ("zh", "en"))


def describe(image, endpoint, api_key, model, timeout, context):
    payload = {
        "model": model,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}},
        ]}],
    }
    try:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        request = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if status in (401, 403):
            raise ServiceUnavailable(f"模型服务认证失败（HTTP {status}）") from None
        if status in (404, 429) or status >= 500:
            raise ServiceUnavailable(f"模型服务不可用（HTTP {status}），请检查地址或稍后重试") from None
        raise ValueError(f"模型请求被拒绝（HTTP {status}）") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        if isinstance(reason, ssl.SSLError):
            raise ServiceUnavailable("模型服务 TLS 校验失败，请检查证书或使用 --ca-file 指定内部 CA") from None
        raise ServiceUnavailable("模型服务连接失败或超时，请检查服务后重试") from None
    except ValueError:
        raise ServiceUnavailable("模型请求配置无效，请检查 LLM_BASE_URL 和 LLM_API_KEY") from None
    try:
        content = json.loads(body)["choices"][0]["message"]["content"].strip()
        lines = content.splitlines()
        if len(lines) >= 3 and lines[0] in ("```json", "```") and lines[-1] == "```":
            content = "\n".join(lines[1:-1])
        result = json.loads(content)
        if not valid_description(result):
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ValueError("模型未返回有效的中英文 JSON 描述") from None
    return {lang: result[lang].strip() for lang in ("zh", "en")}


def atomic_write(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix="." + path.name, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_draft(path, draft):
    atomic_write(path, json.dumps(draft, ensure_ascii=False, indent=2) + "\n")


def load_draft(path):
    draft = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(draft, dict) or draft.get("version") != 1 or not isinstance(draft.get("items"), list) or not isinstance(draft.get("library"), str):
        raise ValueError("draft.json 格式不正确")
    poses = [item.get("pose") for item in draft["items"] if isinstance(item, dict)]
    if len(poses) != len(draft["items"]) or not all(isinstance(pose, str) for pose in poses) or len(set(poses)) != len(poses):
        raise ValueError("draft.json 中的姿势路径无效或重复")
    if not all(isinstance(item.get("source_sha256"), str) and (item.get("preview") is None or isinstance(item["preview"], str)) for item in draft["items"]):
        raise ValueError("draft.json 中的源文件记录不完整")
    return draft


def render_review(path, draft):
    library = Path(draft["library"]).resolve()
    cards = []
    for item in draft["items"]:
        image = ""
        error = item.get("error", "")
        if item.get("preview"):
            try:
                preview = library_file(library, item["preview"])
                encoded = base64.b64encode(png_bytes(preview.read_bytes(), thumbnail=True)).decode("ascii")
                image = f'<img src="data:image/png;base64,{encoded}" alt="姿势预览">'
            except (OSError, ValueError):
                error = error or "预览图无法读取"
        description = item.get("description") if valid_description(item.get("description")) else {}
        cards.append(
            '<article>' + image + '<div><h2>' + html.escape(item["pose"]) + '</h2>'
            + '<p class="path">' + html.escape(item.get("preview") or "无预览图") + '</p>'
            + '<p lang="zh">' + html.escape(description.get("zh", "")) + '</p>'
            + '<p lang="en">' + html.escape(description.get("en", "")) + '</p>'
            + '<p class="error">' + html.escape(error) + '</p></div></article>'
        )
    document = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>姿势描述审核</title><style>
body{max-width:1050px;margin:32px auto;padding:0 20px;font:16px/1.6 system-ui;color:#222;background:#fafafa}
article{display:flex;gap:24px;margin:20px 0;padding:20px;background:white;border:1px solid #ddd;border-radius:8px}
img{width:240px;max-height:320px;object-fit:contain;align-self:flex-start}article div{min-width:0}
h2{font-size:17px;overflow-wrap:anywhere}p{white-space:pre-wrap;overflow-wrap:anywhere}.path{font-size:13px;color:#666}.error{color:#a21}
@media(max-width:650px){article{flex-direction:column}img{max-width:100%}}
</style><h1>姿势描述审核</h1><p>描述仅保存在 draft.json，尚未写回姿势库。请编辑该 JSON 后使用 --render-only 刷新此页。</p>
""" + f'<p>共 {len(draft["items"])} 条 · 模型：{html.escape(draft.get("model", ""))}</p>' + "\n".join(cards) + "</html>\n"
    atomic_write(path, document)


def run(args):
    output = args.output_dir.resolve()
    draft_path = output / "draft.json"
    draft = load_draft(draft_path) if draft_path.exists() else None
    if args.render_only and draft is None:
        raise ValueError("未找到 draft.json，请先生成描述")
    library = (Path(draft["library"]) if args.render_only else args.library).resolve()
    if output.is_relative_to(library):
        raise ValueError("输出目录不能位于姿势库内，请使用 --output-dir 指定其他目录")
    if args.render_only:
        render_review(output / "review.html", draft)
        print(f"审核页：{output / 'review.html'}")
        return 0
    if not library.is_dir():
        raise ValueError(f"姿势库目录不存在：{library}")
    settings = DescriptionSettings.from_env()
    model = settings.model
    if draft is None:
        draft = {"version": 1, "library": str(library), "model": model, "prompt_version": PROMPT_VERSION, "prompt": PROMPT, "items": []}
    elif Path(draft["library"]).resolve() != library or draft.get("model") != model or draft.get("prompt_version") != PROMPT_VERSION:
        raise ValueError("姿势库、模型或提示词版本与已有暂存不一致，请使用新输出目录")
    poses = scan_poses(library)
    if not poses:
        raise ValueError("姿势库中未找到静态姿势 JSON")
    context = ssl.create_default_context(cafile=args.ca_file)
    output.mkdir(parents=True, exist_ok=True)
    entries = {item["pose"]: item for item in draft["items"]}
    processed = skipped = 0
    failed = False
    try:
        for pose, preview in poses:
            relative = pose.relative_to(library).as_posix()
            preview_relative = preview.relative_to(library).as_posix() if preview else None
            image_data = preview.read_bytes() if preview else b""
            fingerprint = hashlib.sha256(pose.read_bytes() + b"\0" + (preview_relative or "").encode("utf-8") + b"\0" + image_data).hexdigest()
            item = entries.get(relative)
            if item is not None:
                if item["source_sha256"] != fingerprint:
                    item["error"] = "源文件已变化，请使用新输出目录重新标注"
                    print(f"保留旧描述：{relative}（源文件已变化）", file=sys.stderr)
                    failed = True
                    save_draft(draft_path, draft)
                    continue
                if valid_description(item.get("description")):
                    item.pop("error", None)
                    skipped += 1
                    continue
            if args.limit is not None and processed >= args.limit:
                break
            if item is None:
                item = {"pose": relative, "preview": preview_relative, "source_sha256": fingerprint}
                entries[relative] = item
                draft["items"].append(item)
            processed += 1
            stop = False
            try:
                if preview is None:
                    raise ValueError("缺少同名预览图")
                image = png_bytes(image_data)
                item["description"] = describe(image, settings.url, settings.api_key, model, args.timeout, context)
                item.pop("error", None)
                print(f"已生成：{relative}", flush=True)
            except ServiceUnavailable as error:
                item["error"] = str(error)
                stop = True
            except OSError:
                item["error"] = "预览图无法解码"
            except ValueError as error:
                item["error"] = str(error)
            save_draft(draft_path, draft)
            if item.get("error"):
                print(f"失败：{relative}：{item['error']}", file=sys.stderr, flush=True)
                failed = True
            if stop:
                break
    except KeyboardInterrupt:
        print("已中断，已完成的描述会保留", file=sys.stderr)
        failed = True
    finally:
        save_draft(draft_path, draft)
        render_review(output / "review.html", draft)
    print(f"本次处理 {processed} 条，跳过已有描述 {skipped} 条。审核页：{output / 'review.html'}")
    return int(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY, help="姿势库目录")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT, help="待审核文件目录，不能位于姿势库内")
    parser.add_argument("--limit", type=int, help="本次最多处理的未完成姿势数量")
    parser.add_argument("--timeout", type=float, default=120, help="单次模型请求超时秒数（默认 120）")
    parser.add_argument("--ca-file", help="HTTPS 内部 CA 证书文件")
    parser.add_argument("--render-only", action="store_true", help="从 draft.json 刷新审核页，不调用模型")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于零")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于零")
    try:
        return run(args)
    except (OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
