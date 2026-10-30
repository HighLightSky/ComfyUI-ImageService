import contextlib
import json
from pathlib import Path
import ssl
import zipfile

import aiohttp
import numpy as np

from .config import EmbeddingSettings
from .errors import ServiceError


def normalize_vectors(values, count, dimensions):
    vectors = np.asarray(values, dtype=np.float32)
    if vectors.shape != (count, dimensions) or not np.isfinite(vectors).all():
        raise ValueError("向量数量、维度或数值无效")
    norms = np.linalg.norm(vectors, axis=1)
    if not np.isfinite(norms).all() or (norms <= 0).any():
        raise ValueError("向量范数无效")
    return vectors / norms[:, None]


class EmbeddingClient:
    def __init__(self, settings):
        self.settings = settings
        self.session = None

    @classmethod
    def from_env(cls):
        return cls(EmbeddingSettings.from_env())

    async def start(self):
        context = ssl.create_default_context(cafile=self.settings.ca_file or None)
        self.session = aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + self.settings.api_key} if self.settings.api_key else {},
            timeout=aiohttp.ClientTimeout(total=self.settings.timeout),
            connector=aiohttp.TCPConnector(limit=2, ssl=context),
        )

    async def close(self):
        if self.session is not None:
            await self.session.close()

    async def embed(self, texts, model, dimensions):
        try:
            async with self.session.post(self.settings.url, json={"model": model, "input": texts, "encoding_format": "float"}, allow_redirects=False) as response:
                if response.status != 200:
                    raise ServiceError(502, "EMBEDDING_UNAVAILABLE", "向量服务拒绝请求或暂不可用")
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(body) + len(chunk) > len(texts) * dimensions * 32 + 65536:
                        raise ServiceError(502, "EMBEDDING_PROTOCOL", "向量服务响应过大")
                    body.extend(chunk)
        except TimeoutError:
            raise ServiceError(504, "EMBEDDING_TIMEOUT", "向量服务请求超时") from None
        except (aiohttp.ClientError, OSError, ValueError):
            raise ServiceError(502, "EMBEDDING_UNAVAILABLE", "向量服务连接或请求配置异常") from None
        try:
            data = json.loads(body)
            if not isinstance(data, dict) or data.get("model") != model or not isinstance(data.get("data"), list) or len(data["data"]) != len(texts):
                raise ValueError
            ordered = [None] * len(texts)
            for row in data["data"]:
                if not isinstance(row, dict):
                    raise ValueError
                index = row.get("index")
                if type(index) is not int or not 0 <= index < len(texts) or ordered[index] is not None:
                    raise ValueError
                ordered[index] = row["embedding"]
            return normalize_vectors(ordered, len(texts), dimensions)
        except (ValueError, KeyError, TypeError, OverflowError):
            raise ServiceError(502, "EMBEDDING_PROTOCOL", "向量服务返回的模型、数量、维度或数值无效") from None


class PoseIndex:
    def __init__(self, vectors, metadata):
        if not isinstance(metadata, dict) or metadata.get("version") != 1:
            raise ValueError("姿势索引版本无效，请重新构建")
        if not isinstance(metadata.get("model"), str) or not metadata["model"].strip() or type(metadata.get("dimensions")) is not int or metadata["dimensions"] <= 0:
            raise ValueError("姿势索引模型或维度无效，请重新构建")
        if not isinstance(metadata.get("query_instruction"), str):
            raise ValueError("姿势索引缺少查询指令")
        records = metadata.get("records")
        if not isinstance(records, list) or not records:
            raise ValueError("姿势索引为空或记录无效")
        ids = set()
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("pose_id"), str) or not record["pose_id"] or record["pose_id"] in ids:
                raise ValueError("姿势索引包含无效或重复的姿势 ID")
            description = record.get("description")
            if not isinstance(description, dict) or not all(isinstance(description.get(lang), str) and description[lang].strip() for lang in ("zh", "en")) or not isinstance(record.get("pose"), dict):
                raise ValueError("姿势索引缺少描述或姿势 JSON")
            ids.add(record["pose_id"])
        if not isinstance(vectors, np.ndarray) or vectors.dtype != np.float32:
            raise ValueError("姿势索引必须使用 float32 向量")
        normalized = normalize_vectors(vectors, len(records), metadata["dimensions"])
        if not np.allclose(vectors, normalized, rtol=1e-4, atol=1e-6):
            raise ValueError("姿势索引向量未归一化，请重新构建")
        self.vectors = vectors
        self.metadata = metadata
        self.records = records
        self.library_indices = {"default": [], "mixamo": [], "all": list(range(len(records)))}
        for index, record in enumerate(records):
            source = record["pose"].get("_library", {}).get("source", {})
            library = "mixamo" if source.get("dataset") == "Linzhan/Mixamo-Animations-Characters" else "default"
            self.library_indices[library].append(index)

    @classmethod
    def load(cls, path):
        try:
            with np.load(path, allow_pickle=False) as data:
                return cls(data["vectors"], json.loads(data["metadata"].item()))
        except (OSError, ValueError, KeyError, TypeError, EOFError, zipfile.BadZipFile):
            raise ValueError("姿势索引无法加载或格式无效，请重新构建") from None

    def search(self, query_vector, top_k, library="default"):
        scores = self.vectors @ query_vector
        order = sorted(self.library_indices[library], key=lambda index: (-float(scores[index]), self.records[index]["pose_id"]))
        return [{
            "pose_id": self.records[index]["pose_id"],
            "score": float(scores[index]),
            "description": self.records[index]["description"],
            "pose": self.records[index]["pose"],
        } for index in order[:top_k]]


class PoseSearch:
    def __init__(self, index_path):
        self.index_path = index_path
        self.index = None
        self.client = None
        self.active = 0
        self.unavailable_reason = "姿势检索未就绪，请检查索引和向量服务配置"

    async def start(self):
        if not self.index_path:
            return
        try:
            index = PoseIndex.load(Path(self.index_path))
            client = EmbeddingClient.from_env()
            if index.metadata["model"] != client.settings.model or index.metadata["dimensions"] != client.settings.dimensions:
                raise ValueError("向量服务模型或维度与姿势索引不匹配，请检查配置或重建索引")
            await client.start()
        except ValueError as error:
            self.unavailable_reason = str(error)
            return
        except OSError:
            self.unavailable_reason = "姿势检索文件不可用，请检查索引和 IMAGE_SERVICE_POSE_EMBEDDING_CA_FILE"
            return
        self.index = index
        self.client = client

    async def close(self):
        if self.client is not None:
            await self.client.close()

    @contextlib.contextmanager
    def admit(self):
        if self.index is None:
            raise ServiceError(503, "POSE_INDEX_NOT_READY", self.unavailable_reason)
        if self.active >= 2:
            raise ServiceError(429, "POSE_SEARCH_BUSY", "姿势检索繁忙，请稍后重试")
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1

    async def search(self, query, top_k, library="default"):
        instruction = self.index.metadata["query_instruction"]
        text = f"Instruct: {instruction}\nQuery: {query}" if instruction else query
        vectors = await self.client.embed([text], self.index.metadata["model"], self.index.metadata["dimensions"])
        return self.index.search(vectors[0], top_k, library)
