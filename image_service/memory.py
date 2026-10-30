import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field

from .errors import ServiceError
from .imaging import inspect_image


@dataclass
class MemoryTask:
    expires: float
    keys: set = field(default_factory=set)
    reserved: int = 0
    uploading: bool = False
    pixels: int = 0


class ImageMemory:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.tasks = {}
        self.images = {}
        self.submitting = set()
        self.used = 0

    def create(self, task_id):
        if str(uuid.UUID(task_id)) != task_id:
            raise ValueError("Invalid task ID")
        with self.lock:
            if task_id in self.tasks:
                raise ServiceError(409, "TASK_EXISTS", "任务已存在")
            if len(self.tasks) >= self.settings.queue_size + 1:
                raise ServiceError(429, "MEMORY_BUSY", "内存任务数超限")
            self.tasks[task_id] = MemoryTask(time.monotonic() + self.settings.resource_ttl)

    def get_task(self, task_id):
        task = self.tasks.get(task_id)
        if task is None:
            raise ServiceError(410, "RESOURCE_EXPIRED", "图像资源不存在或已释放")
        return task

    def reserve(self, task_id, size):
        with self.lock:
            task = self.get_task(task_id)
            if self.used + size > self.settings.comfy_memory_bytes:
                raise ServiceError(429, "MEMORY_BUSY", "图像内存预算不足")
            self.used += size
            task.reserved += size
            task.expires = time.monotonic() + self.settings.resource_ttl

    def begin_upload(self, task_id):
        with self.lock:
            task = self.get_task(task_id)
            if task.uploading or len(task.keys) >= 10:
                raise ServiceError(409, "INVALID_UPLOAD", "任务不能接收更多图片")
            task.uploading = True

    def finish_upload(self, task_id, data):
        pixels = inspect_image(data, self.settings, load=False)
        with self.lock:
            task = self.get_task(task_id)
            if task.pixels + pixels > self.settings.max_total_pixels:
                raise ServiceError(413, "TOO_MANY_PIXELS", "参考图总像素数超限")
            # RGB float32 tensor plus bounded Pillow/numpy conversion workspace.
            self.reserve(task_id, pixels * 16)
            key = secrets.token_urlsafe(32)
            self.images[key] = (task_id, data)
            task.keys.add(key)
            task.pixels += pixels
            task.uploading = False
            return key

    def take(self, key):
        with self.lock:
            item = self.images.pop(key, None)
            if item is None:
                raise ServiceError(410, "RESOURCE_EXPIRED", "图像资源不存在、已读取或已释放")
            self.get_task(item[0])
            return item[1]

    def release(self, task_id):
        with self.lock:
            task = self.tasks.pop(task_id, None)
            if task is not None:
                for key in task.keys:
                    self.images.pop(key, None)
                self.used -= task.reserved

    def reap(self, active_ids):
        with self.lock:
            for task_id, task in list(self.tasks.items()):
                if task_id not in active_ids and task_id not in self.submitting and task.expires < time.monotonic() and not task.uploading:
                    self.release(task_id)

    def stats(self):
        with self.lock:
            return {"tasks": len(self.tasks), "images": len(self.images), "reserved_bytes": self.used}
