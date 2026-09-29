from __future__ import annotations

import hashlib
import math
from array import array
from pathlib import Path
from typing import List

from .kv_store import SqliteKeyValueStore
from .models import ChunkRecord
from .openai_client import OpenAIClient
from .value_parsers import normalize_whitespace


def _pack(embedding: List[float]) -> bytes:
    return array("f", embedding).tobytes()


def _unpack(blob: bytes) -> List[float]:
    values = array("f")
    values.frombytes(blob)
    return values.tolist()


class EmbeddingCache:
    def __init__(self, cache_file: Path | str) -> None:
        self.cache_file = Path(cache_file)
        self._store = SqliteKeyValueStore(self.cache_file)

    def get(self, model: str, text: str) -> List[float] | None:
        blob = self._store.get(self._key(model, text))
        return _unpack(blob) if blob is not None else None

    def set(self, model: str, text: str, embedding: List[float]) -> None:
        self._store.set(self._key(model, text), _pack(embedding))

    def save(self) -> None:
        self._store.commit()

    def close(self) -> None:
        self._store.close()

    def _key(self, model: str, text: str) -> str:
        normalized = normalize_whitespace(text)
        digest = hashlib.sha256(f"{model}\n{normalized}".encode("utf-8")).hexdigest()
        return digest


class DenseRetriever:
    def __init__(self, client: OpenAIClient, embedding_model: str, cache: EmbeddingCache, top_k: int = 5) -> None:
        self.client = client
        self.embedding_model = embedding_model
        self.cache = cache
        self.top_k = top_k

    def rank_chunks(self, query_text: str, chunks: List[ChunkRecord]) -> List[ChunkRecord]:
        if not chunks:
            return []

        chunk_texts = [chunk.text for chunk in chunks]
        query_embedding = self._get_embedding(query_text)
        chunk_embeddings = self._get_embeddings(chunk_texts)

        scored = []
        for chunk, embedding in zip(chunks, chunk_embeddings):
            score = cosine_similarity(query_embedding, embedding)
            scored.append((score, chunk.source_priority, -chunk.order, chunk))

        scored.sort(reverse=True)
        return [item[3] for item in scored[: self.top_k]]

    def _get_embeddings(self, texts: List[str]) -> List[List[float]]:
        results: List[List[float] | None] = [None] * len(texts)
        missing_indices: List[int] = []
        missing_texts: List[str] = []

        for index, text in enumerate(texts):
            cached = self.cache.get(self.embedding_model, text)
            if cached is None:
                missing_indices.append(index)
                missing_texts.append(text)
            else:
                results[index] = cached

        if missing_texts:
            embeddings = self.client.create_embeddings(missing_texts, cache_hits=len(texts) - len(missing_texts))
            for index, embedding in zip(missing_indices, embeddings):
                results[index] = embedding
                self.cache.set(self.embedding_model, texts[index], embedding)
            self.cache.save()

        return [embedding or [] for embedding in results]

    def _get_embedding(self, text: str) -> List[float]:
        cached = self.cache.get(self.embedding_model, text)
        if cached is not None:
            return cached
        embedding = self.client.create_embeddings([text], cache_hits=0)[0]
        self.cache.set(self.embedding_model, text, embedding)
        self.cache.save()
        return embedding


def cosine_similarity(left: List[float], right: List[float]) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return numerator / (left_norm * right_norm)
