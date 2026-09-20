import json
import os
import threading
from functools import lru_cache
from pathlib import Path

import httpx
import numpy as np

from .store import atomic_write, revision
from .languages import target_paragraphs


_local_inference_lock = threading.Lock()


@lru_cache(maxsize=2)
def local_model(name):
    # Jobs use preinstalled models; metadata probes must never stall a task.
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    try:
        from sentence_transformers import SentenceTransformer
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise ValueError('请安装 pip install -e ".[local-rag]" 或配置 TRANSMUX_EMBEDDING_URL') from exc
    try:
        location = str(Path(name).resolve()) if Path(name).is_dir() else snapshot_download(name, local_files_only=True)
        return SentenceTransformer(location, local_files_only=True)
    except Exception as exc:
        raise ValueError(f'本地向量模型加载失败：{name}。请预先安装完整模型缓存或配置 embedding 服务；任务不会在线下载模型。详情：{exc}') from exc


class Embeddings:
    def __init__(self):
        self.url = os.getenv("TRANSMUX_EMBEDDING_URL", "").rstrip("/")
        self.model = os.getenv("TRANSMUX_EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        self.identity = self.url + "|" + self.model

    def encode(self, texts):
        if self.url:
            headers = {}
            if os.getenv("TRANSMUX_EMBEDDING_KEY"):
                headers["Authorization"] = "Bearer " + os.environ["TRANSMUX_EMBEDDING_KEY"]
            vectors = []
            with httpx.Client(timeout=120) as client:
                for start in range(0, len(texts), 32):
                    batch = texts[start:start + 32]
                    response = client.post(self.url + "/embeddings", headers=headers,
                                           json={"model": self.model, "input": batch})
                    response.raise_for_status()
                    data = sorted(response.json()["data"], key=lambda row: row["index"])
                    if [r["index"] for r in data] != list(range(len(batch))):
                        raise ValueError("Embedding 返回数量或索引错误")
                    vectors.extend(row["embedding"] for row in data)
        else:
            # Shared local model initialization/inference is protected; agent jobs remain independent.
            if not _local_inference_lock.acquire(timeout=120):
                raise TimeoutError('等待本地向量模型超过 120 秒；请检查其他索引任务或向量模型状态')
            try:
                vectors = local_model(self.model).encode(texts, normalize_embeddings=True)
            finally:
                _local_inference_lock.release()
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(texts) or not np.isfinite(matrix).all():
            raise ValueError("Embedding 返回无效向量")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if (norms == 0).any():
            raise ValueError("Embedding 返回零向量")
        return matrix / norms


class Rag:
    def __init__(self, embeddings=None):
        self.embeddings = embeddings or Embeddings()

    @staticmethod
    def fingerprint(files):
        return revision(json.dumps([(f["id"], f["path"]) for f in files], sort_keys=True))

    def build(self, work, files, target="en"):
        chunks = []
        records, counts = target_paragraphs(work, files, target)
        for record in records:
            block = record["text"]
            for offset in range(0, len(block), 240):
                chunks.append(dict(record, text=block[offset:offset + 280]))
        vectors = self.embeddings.encode([c["text"] for c in chunks]).tolist() if chunks else []
        data = {"version": 2, "target_language": target, "model": self.embeddings.identity,
                "fingerprint": self.fingerprint(files), "chunks": chunks, "vectors": vectors, "counts": counts}
        atomic_write(work / "rag" / "index.json", json.dumps(data, ensure_ascii=False))
        return {"chunks": len(chunks), "files": len(files), **counts}

    def load(self, work, files, target="en"):
        path = work / "rag" / "index.json"
        if not path.exists():
            raise ValueError("请先构建 RAG 索引")
        data = json.loads(path.read_text())
        if (data.get("version") != 2 or data.get("target_language") != target
                or data["model"] != self.embeddings.identity or data["fingerprint"] != self.fingerprint(files)
                or any(c.get("language") != target for c in data["chunks"])):
            raise ValueError("语料或向量模型已变化，请重新构建 RAG")
        return data

    def search_many(self, work, files, queries, top_k=3, target="en"):
        data = self.load(work, files, target)
        if not data["chunks"]:
            return [[] for _ in queries]
        segments = [[q[offset:offset + 280] for offset in range(0, len(q), 240)] for q in queries]
        encoded = self.embeddings.encode([part for query in segments for part in query])
        query_vectors, offset = [], 0
        for query in segments:
            mean = encoded[offset:offset + len(query)].mean(axis=0)
            query_vectors.append(mean / max(float(np.linalg.norm(mean)), 1e-12))
            offset += len(query)
        query_vectors = np.asarray(query_vectors)
        scores = query_vectors @ np.asarray(data["vectors"], dtype=np.float32).T
        return [[dict(data["chunks"][int(i)], score=round(float(row[i]), 4))
                 for i in np.argsort(-row)[:top_k]] for row in scores]
