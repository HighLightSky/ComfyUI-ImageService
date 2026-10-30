import os
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

from .paths import POSE_ASSET_ROOT


def validate_endpoint(url, name):
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ValueError(f"{name} must be a complete HTTP(S) endpoint without credentials") from None
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{name} must be a complete HTTP(S) endpoint without credentials")


@dataclass(frozen=True)
class EmbeddingSettings:
    url: str
    model: str
    dimensions: int
    api_key: str = ""
    ca_file: str = ""
    timeout: int = 30
    query_instruction: str = ""

    def __post_init__(self):
        prefix = "IMAGE_SERVICE_POSE_EMBEDDING_"
        validate_endpoint(self.url, prefix + "URL")
        if not self.model.strip():
            raise ValueError(prefix + "MODEL is required")
        if type(self.dimensions) is not int or self.dimensions <= 0:
            raise ValueError(prefix + "DIMENSIONS must be a positive integer")
        if self.timeout <= 0:
            raise ValueError(prefix + "TIMEOUT must be positive")

    @classmethod
    def from_env(cls):
        prefix = "IMAGE_SERVICE_POSE_EMBEDDING_"
        values = {name: os.environ.get(prefix + name.upper(), "") for name in ("url", "model", "api_key", "ca_file", "query_instruction")}
        for name, default in (("dimensions", "0"), ("timeout", "30")):
            try:
                values[name] = int(os.environ.get(prefix + name.upper(), default))
            except ValueError:
                raise ValueError(prefix + name.upper() + " must be a positive integer") from None
        return cls(**values)


@dataclass(frozen=True)
class DescriptionSettings:
    url: str
    model: str
    api_key: str = ""

    def __post_init__(self):
        validate_endpoint(self.url, "LLM_BASE_URL")
        if not self.model.strip():
            raise ValueError("LLM_MODEL is required")

    @classmethod
    def from_env(cls):
        return cls(url=os.environ.get("LLM_BASE_URL", ""), model=os.environ.get("LLM_MODEL", ""), api_key=os.environ.get("LLM_API_KEY", ""))


@dataclass(frozen=True)
class Settings:
    api_token: str = ""
    internal_token: str = ""
    api_port: int = 8000
    comfy_port: int = 8188
    queue_size: int = 2
    max_image_bytes: int = 16 * 1024**2
    max_request_bytes: int = 128 * 1024**2
    max_image_pixels: int = 4_194_304
    max_total_pixels: int = 16_777_216
    max_dimension: int = 8192
    gateway_memory_bytes: int = 512 * 1024**2
    comfy_memory_bytes: int = 512 * 1024**2
    max_output_bytes: int = 64 * 1024**2
    max_output_pixels: int = 8_388_608
    max_prompt_chars: int = 16_384
    max_parts: int = 64
    max_header_bytes: int = 8192
    upload_timeout: int = 60
    queue_timeout: int = 600
    execution_timeout: int = 900
    cleanup_timeout: int = 30
    resource_ttl: int = 120
    pose_index_path: str = ""
    pose_browser_path: str = ""
    pose_asset_root: str = str(POSE_ASSET_ROOT)
    pose_render_timeout: int = 30
    max_pose_bytes: int = 1024**2

    @classmethod
    def from_env(cls):
        values = {}
        for field in fields(cls):
            value = os.environ.get("IMAGE_SERVICE_" + field.name.upper())
            if value is not None:
                values[field.name] = value if isinstance(field.default, str) else int(value)
        settings = cls(**values)
        for name in ("api_token", "internal_token"):
            if len(getattr(settings, name)) < 32:
                raise ValueError(f"IMAGE_SERVICE_{name.upper()} must contain at least 32 characters")
        if settings.api_token == settings.internal_token:
            raise ValueError("External and internal tokens must differ")
        for field in fields(cls):
            value = getattr(settings, field.name)
            if isinstance(value, int) and value < (0 if field.name == "queue_size" else 1):
                raise ValueError(f"Invalid setting: {field.name}")
        if not 1024 <= settings.api_port <= 65535 or not 1024 <= settings.comfy_port <= 65535:
            raise ValueError("Service ports must be between 1024 and 65535")
        if settings.api_port == settings.comfy_port:
            raise ValueError("Service ports must differ")
        return settings

    @property
    def comfy_url(self):
        return f"http://127.0.0.1:{self.comfy_port}"


TEMPLATE_PATH = Path(__file__).with_name("workflow_api.json")
POSE_TEMPLATE_PATH = Path(__file__).with_name("workflow_pose_api.json")
