import asyncio
import logging
from io import BytesIO

from pypdf import PdfReader

from app.core.config import settings
from app.services.milvus_service import milvus_service
from app.services.ollama_client import ollama_client


logger = logging.getLogger(__name__)


def chunk_text(text: str, chunk_size: int = 1800, overlap: int = 300) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        chunks.append(text[start:end])
        start += max(1, chunk_size - overlap)
    return [c.strip() for c in chunks if c.strip()]


class RagService:
    def parse_document(self, filename: str, content: bytes) -> str:
        lower = filename.lower()
        if lower.endswith(".txt") or lower.endswith(".md"):
            return content.decode("utf-8", errors="ignore")
        if lower.endswith(".pdf"):
            reader = PdfReader(BytesIO(content))
            return "\n".join([page.extract_text() or "" for page in reader.pages])
        raise ValueError("Unsupported file format. Use PDF, TXT, MD")

    async def ingest_document(self, user_id: str, filename: str, content: bytes) -> int:
        text = self.parse_document(filename, content)
        chunks = chunk_text(text)
        if not chunks:
            return 0

        concurrency = max(1, int(settings.RAG_EMBEDDING_CONCURRENCY))
        semaphore = asyncio.Semaphore(concurrency)

        async def embed_one(index: int, chunk: str) -> tuple[int, list[float]]:
            async with semaphore:
                vector = await ollama_client.embeddings(chunk)
                return index, vector

        embedded = await asyncio.gather(*(embed_one(index, chunk) for index, chunk in enumerate(chunks)))
        embedded.sort(key=lambda item: item[0])
        vectors = [vector for _, vector in embedded]
        milvus_service.insert_chunks(user_id=user_id, chunks=chunks, vectors=vectors, source_doc=filename)
        return len(chunks)

    async def retrieve_context(self, user_id: str, query: str, top_k: int = 5) -> list[dict]:
        try:
            query_embedding = await ollama_client.embeddings(query)
            return milvus_service.search(user_id=user_id, query_embedding=query_embedding, top_k=top_k)
        except Exception as exc:
            logger.warning("rag retrieve_context skipped: %s", exc)
            return []


rag_service = RagService()
