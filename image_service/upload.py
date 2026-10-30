import asyncio
import json
from dataclasses import dataclass

from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

from .errors import ServiceError
from .imaging import inspect_image, run_image_operation


@dataclass
class Upload:
    images: list
    prompt: str
    ignored: int
    pose: dict | None = None


class MultipartUpload:
    def __init__(self, content_type, settings, reserve):
        self.settings = settings
        self.reserve = reserve
        self.images = []
        self.prompt = None
        self.pose = None
        self.image_count = 0
        self.part_count = 0
        self.total = 0
        self.ended = False
        kind, options = parse_options_header(content_type)
        boundary = options.get(b"boundary", b"")
        if kind != b"multipart/form-data" or not 1 <= len(boundary) <= 200:
            self.bad()
        callbacks = {name: getattr(self, name) for name in (
            "on_part_begin", "on_header_field", "on_header_value", "on_header_end",
            "on_headers_finished", "on_part_data", "on_part_end", "on_end",
        )}
        self.parser = MultipartParser(boundary, callbacks)

    def bad(self):
        raise ServiceError(400, "INVALID_MULTIPART", "请求必须包含 images 文件字段及一个 prompt 字段")

    def on_part_begin(self):
        self.part_count += 1
        if self.part_count > self.settings.max_parts:
            raise ServiceError(413, "TOO_MANY_PARTS", "multipart 字段数量超限")
        self.header_bytes = 0
        self.field = bytearray()
        self.value = bytearray()
        self.headers = {}
        self.buffer = bytearray()
        self.part_size = 0
        self.name = None
        self.keep = False

    def header(self, target, data, start, end):
        self.header_bytes += end - start
        if self.header_bytes > self.settings.max_header_bytes:
            raise ServiceError(413, "HEADERS_TOO_LARGE", "multipart 头部过大")
        target.extend(data[start:end])

    def on_header_field(self, data, start, end):
        self.header(self.field, data, start, end)

    def on_header_value(self, data, start, end):
        self.header(self.value, data, start, end)

    def on_header_end(self):
        name = bytes(self.field).lower()
        if name in self.headers:
            self.bad()
        self.headers[name] = bytes(self.value)
        self.field.clear()
        self.value.clear()

    def on_headers_finished(self):
        disposition, options = parse_options_header(self.headers.get(b"content-disposition", b""))
        self.name = options.get(b"name")
        if disposition != b"form-data":
            self.bad()
        if self.name == b"images" and b"filename" in options:
            self.image_count += 1
            self.keep = self.image_count <= 10
        elif self.name == b"prompt" and b"filename" not in options and self.prompt is None:
            self.keep = True
        elif self.name == b"pose" and b"filename" not in options and self.pose is None:
            self.keep = True
        else:
            self.bad()

    def on_part_data(self, data, start, end):
        self.part_size += end - start
        if not self.keep:
            return
        limit = self.settings.max_image_bytes if self.name == b"images" else self.settings.max_pose_bytes if self.name == b"pose" else self.settings.max_prompt_chars * 4
        if self.part_size > limit:
            raise ServiceError(413, "POSE_TOO_LARGE" if self.name == b"pose" else "PART_TOO_LARGE", "上传字段大小超限")
        self.reserve(end - start)
        self.buffer.extend(data[start:end])

    def on_part_end(self):
        if self.keep and self.name == b"images":
            self.images.append(self.buffer)
        elif self.keep and self.name == b"pose":
            try:
                self.pose = json.loads(self.buffer, parse_constant=invalid_constant)
                if not isinstance(self.pose, dict) or not any(key in self.pose for key in ("bones", "characters", "poses", "image_poses")):
                    raise ValueError
            except (ValueError, UnicodeError, RecursionError):
                raise ServiceError(400, "INVALID_POSE", "pose 必须是有效的姿态 JSON 对象") from None
        elif self.keep:
            try:
                self.prompt = self.buffer.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                self.bad()
            if not self.prompt.strip() or len(self.prompt) > self.settings.max_prompt_chars:
                raise ServiceError(400, "INVALID_PROMPT", "提示词为空或长度超限")
        self.buffer = bytearray()

    def on_end(self):
        self.ended = True

    def feed(self, data):
        self.total += len(data)
        if self.total > self.settings.max_request_bytes:
            raise ServiceError(413, "REQUEST_TOO_LARGE", "请求大小超限")
        try:
            self.parser.write(data)
        except MultipartParseError:
            self.bad()

    def finish(self):
        self.parser.finalize()
        if not self.ended or self.prompt is None or not self.images:
            self.bad()
        return Upload(self.images, self.prompt, max(0, self.image_count - 10), self.pose)


def invalid_constant(value):
    raise ValueError("Non-finite JSON number")


async def receive_upload(request, settings, reserve):
    length = request.headers.get("content-length")
    if length is not None:
        try:
            length = int(length)
        except ValueError:
            raise ServiceError(400, "INVALID_MULTIPART", "Content-Length 无效") from None
        if length < 0:
            raise ServiceError(400, "INVALID_MULTIPART", "Content-Length 无效")
        if length > settings.max_request_bytes:
            raise ServiceError(413, "REQUEST_TOO_LARGE", "请求大小超限")
    parser = MultipartUpload(request.headers.get("content-type", ""), settings, reserve)
    try:
        async with asyncio.timeout(settings.upload_timeout):
            async for chunk in request.stream():
                # Bound parser callback work even when an ASGI server supplies a large chunk.
                for start in range(0, len(chunk), 65536):
                    parser.feed(chunk[start:start + 65536])
    except TimeoutError:
        raise ServiceError(408, "UPLOAD_TIMEOUT", "上传超时") from None
    upload = parser.finish()
    total_pixels = 0
    for data in upload.images:
        total_pixels += await run_image_operation(inspect_image, data, settings)
        if total_pixels > settings.max_total_pixels:
            raise ServiceError(413, "TOO_MANY_PIXELS", "参考图总像素数超限")
    return upload
