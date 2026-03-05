import asyncio
import logging
from io import BytesIO
from time import monotonic

from pypdf import PdfReader

from app.core.config import settings
from app.services.milvus_service import milvus_service
from app.services.ollama_client import ollama_client


logger = logging.getLogger(__name__)

DOC_EMBEDDING_UNAVAILABLE = "Document embedding is temporarily unavailable"
DOC_SEARCH_EMBEDDING_UNAVAILABLE = "Document search embedding is temporarily unavailable"


def chunk_text(text: str, chunk_size: int = 1800, overlap: int = 300) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        chunks.append(text[start:end])
        start += max(1, chunk_size - overlap)
    return [c.strip() for c in chunks if c.strip()]


class RagService:
    def __init__(self) -> None:
        self._embedding_cooldown_until = 0.0
        self._query_embedding_cache: dict[str, tuple[float, list[float]]] = {}

    @staticmethod
    def _is_zero_vector(vector: list[float]) -> bool:
        if not vector:
            return True
        return all(abs(float(value)) < 1e-12 for value in vector)

    def _embedding_timeout_seconds(self) -> float:
        return max(1.0, float(settings.RAG_EMBEDDING_TIMEOUT_SECONDS))

    def _in_embedding_cooldown(self) -> bool:
        return monotonic() < self._embedding_cooldown_until

    def _trigger_embedding_cooldown(self) -> None:
        cooldown = max(1.0, float(settings.RAG_EMBEDDING_COOLDOWN_SECONDS))
        self._embedding_cooldown_until = monotonic() + cooldown

    @staticmethod
    def _normalize_query_for_cache(query: str) -> str:
        return " ".join(str(query or "").strip().lower().split())

    def _cache_get_query_embedding(self, query: str) -> list[float] | None:
        key = self._normalize_query_for_cache(query)
        if not key:
            return None
        entry = self._query_embedding_cache.get(key)
        if not entry:
            return None
        ts, vector = entry
        ttl = max(1, int(settings.RAG_QUERY_EMBED_CACHE_TTL_SECONDS))
        if monotonic() - ts > ttl:
            self._query_embedding_cache.pop(key, None)
            return None
        return vector

    def _cache_put_query_embedding(self, query: str, vector: list[float]) -> None:
        key = self._normalize_query_for_cache(query)
        if not key:
            return
        max_items = max(10, int(settings.RAG_QUERY_EMBED_CACHE_MAX_ITEMS))
        if len(self._query_embedding_cache) >= max_items:
            oldest_key = min(self._query_embedding_cache, key=lambda item: self._query_embedding_cache[item][0])
            self._query_embedding_cache.pop(oldest_key, None)
        self._query_embedding_cache[key] = (monotonic(), vector)

    async def _embed_with_timeout(self, text: str) -> list[float]:
        return await asyncio.wait_for(
            ollama_client.embeddings(text),
            timeout=self._embedding_timeout_seconds(),
        )

    def parse_document(self, filename: str, content: bytes) -> str:
        lower = filename.lower()
        if lower.endswith(".txt") or lower.endswith(".md"):
            return content.decode("utf-8", errors="ignore")
        if lower.endswith(".pdf"):
            reader = PdfReader(BytesIO(content))
            return "\n".join([page.extract_text() or "" for page in reader.pages])
        raise ValueError("Unsupported file format. Use PDF, TXT, MD")

    async def ingest_document(self, user_id: str, filename: str, content: bytes) -> int:
        if self._in_embedding_cooldown():
            raise RuntimeError(DOC_EMBEDDING_UNAVAILABLE)

        text = self.parse_document(filename, content)
        chunks = chunk_text(text)
        if not chunks:
            return 0

        concurrency = max(1, int(settings.RAG_EMBEDDING_CONCURRENCY))
        semaphore = asyncio.Semaphore(concurrency)

        async def embed_one(index: int, chunk: str) -> tuple[int, list[float]]:
            async with semaphore:
                vector = await self._embed_with_timeout(chunk)
                return index, vector

        embedded = await asyncio.gather(
            *(embed_one(index, chunk) for index, chunk in enumerate(chunks)),
            return_exceptions=True,
        )
        failed_embeddings = sum(1 for item in embedded if isinstance(item, Exception))
        if failed_embeddings:
            logger.warning("rag ingest: %s chunk embeddings failed", failed_embeddings)

        successful_embeddings = [item for item in embedded if not isinstance(item, Exception)]
        if not successful_embeddings:
            self._trigger_embedding_cooldown()
            raise RuntimeError(DOC_EMBEDDING_UNAVAILABLE)

        embedded = successful_embeddings
        embedded.sort(key=lambda item: item[0])
        valid_pairs = [(chunks[index], vector) for index, vector in embedded if not self._is_zero_vector(vector)]
        if not valid_pairs:
            self._trigger_embedding_cooldown()
            raise RuntimeError(DOC_EMBEDDING_UNAVAILABLE)

        valid_chunks = [chunk for chunk, _ in valid_pairs]
        vectors = [vector for _, vector in valid_pairs]
        milvus_service.insert_chunks(user_id=user_id, chunks=valid_chunks, vectors=vectors, source_doc=filename)
        return len(valid_chunks)

    async def retrieve_context(self, user_id: str, query: str, top_k: int = 5) -> list[dict]:
        if self._in_embedding_cooldown():
            raise RuntimeError(DOC_SEARCH_EMBEDDING_UNAVAILABLE)
        try:
            query_embedding = self._cache_get_query_embedding(query)
            if query_embedding is None:
                query_embedding = await self._embed_with_timeout(query)
                if self._is_zero_vector(query_embedding):
                    self._trigger_embedding_cooldown()
                    raise RuntimeError(DOC_SEARCH_EMBEDDING_UNAVAILABLE)
                self._cache_put_query_embedding(query, query_embedding)
            if self._is_zero_vector(query_embedding):
                self._trigger_embedding_cooldown()
                raise RuntimeError(DOC_SEARCH_EMBEDDING_UNAVAILABLE)
            return milvus_service.search(user_id=user_id, query_embedding=query_embedding, top_k=top_k)
        except Exception as exc:
            logger.warning("rag retrieve_context skipped: %s", exc)
            if isinstance(exc, RuntimeError):
                raise
            self._trigger_embedding_cooldown()
            raise RuntimeError(DOC_SEARCH_EMBEDDING_UNAVAILABLE) from exc

    async def list_documents(self, user_id: str, limit: int = 1000) -> list[dict]:
        await asyncio.sleep(0)
        try:
            return milvus_service.list_user_documents(user_id=user_id, limit=limit)
        except Exception as exc:
            logger.warning("rag list_documents skipped: %s", exc)
            raise RuntimeError("Document list is temporarily unavailable") from exc

    async def delete_document(self, user_id: str, source_doc: str) -> int:
        await asyncio.sleep(0)
        normalized = str(source_doc or "").strip()
        if not normalized:
            raise ValueError("source_doc is required")
        try:
            return milvus_service.delete_document_chunks(user_id=user_id, source_doc=normalized)
        except Exception as exc:
            logger.warning("rag delete_document skipped: %s", exc)
            raise RuntimeError("Document delete is temporarily unavailable") from exc

    async def delete_all_documents(self, user_id: str) -> int:
        await asyncio.sleep(0)
        try:
            return milvus_service.delete_user_chunks(user_id=user_id)
        except Exception as exc:
            logger.warning("rag delete_all_documents skipped: %s", exc)
            raise RuntimeError("Document delete is temporarily unavailable") from exc


rag_service = RagService()
