"""VectorToolRegistry — semantic tool search & storage via Milvus.

Maintains a dedicated Milvus collection ``tool_vectors`` that stores
embedding representations of every registered tool (dynamic tools,
integrations, future built-ins). Before each LLM planner call the
retriever node queries this collection to find the most relevant tools
for the user's query, enabling the system to scale to thousands of
tools without stuffing every signature into the prompt.

Milvus collection schema::

    pk          : INT64      (auto_id primary key)
    vector      : FLOAT_VEC  (dim = EMBEDDING_DIM from settings)
    tool_name   : VARCHAR    (unique per user, e.g. "dyn:weather_api")
    user_id     : VARCHAR    (UUID string — ownership boundary)
    tool_type   : VARCHAR    ("dynamic" | "integration" | "builtin")
    description : VARCHAR    (human-readable, used for embedding)
    endpoint    : VARCHAR    (HTTP endpoint or empty for builtins)
    method      : VARCHAR    (GET/POST/...)
    param_schema: JSON       (JSON Schema of tool parameters)
    metadata    : JSON       (extra data — response_hint, headers, etc.)
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from app.core.config import settings

logger = logging.getLogger(__name__)

_COLLECTION_NAME = "tool_vectors"
_FULL_OUTPUT_FIELDS = [
    "tool_name", "user_id", "tool_type", "description",
    "endpoint", "method", "param_schema", "metadata",
]
_BASIC_OUTPUT_FIELDS = [
    "tool_name", "user_id", "tool_type", "description",
    "endpoint", "method",
]
_TOKEN_RE = re.compile(r"[a-zA-Zа-яА-Я0-9_]+")


class VectorToolRegistry:
    """Semantic tool search & CRUD backed by Milvus."""

    def __init__(self) -> None:
        self._collection: Collection | None = None

    # -------------------------------------------------------------- #
    # Connection & collection management
    # -------------------------------------------------------------- #

    def _connect(self) -> None:
        connections.connect(
            alias="default",
            host=settings.MILVUS_HOST,
            port=str(settings.MILVUS_PORT),
        )

    def _ensure_collection(self) -> Collection:
        """Create the ``tool_vectors`` collection if it doesn't exist."""
        if self._collection is not None:
            return self._collection

        self._connect()

        if utility.has_collection(_COLLECTION_NAME):
            self._collection = Collection(_COLLECTION_NAME)
            self._collection.load()
            return self._collection

        fields = [
            FieldSchema(name="pk", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=settings.EMBEDDING_DIM),
            FieldSchema(name="tool_name", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="user_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="tool_type", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="description", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="endpoint", dtype=DataType.VARCHAR, max_length=2048),
            FieldSchema(name="method", dtype=DataType.VARCHAR, max_length=10),
            FieldSchema(name="param_schema", dtype=DataType.JSON),
            FieldSchema(name="metadata", dtype=DataType.JSON),
        ]
        schema = CollectionSchema(fields=fields, description="Tool vector storage for semantic search")
        collection = Collection(_COLLECTION_NAME, schema=schema)
        collection.create_index(
            field_name="vector",
            index_params={
                "index_type": "HNSW",
                "metric_type": "COSINE",
                "params": {"M": 16, "efConstruction": 200},
            },
        )
        collection.load()
        self._collection = collection
        logger.info("Created Milvus collection '%s' with HNSW index", _COLLECTION_NAME)
        return collection

    # -------------------------------------------------------------- #
    # Embedding generation
    # -------------------------------------------------------------- #

    @staticmethod
    async def _embed(text: str) -> list[float]:
        """Generate embedding for a tool description."""
        from app.llm import llm_provider
        return await llm_provider.embeddings(text)

    # -------------------------------------------------------------- #
    # Registration (upsert)
    # -------------------------------------------------------------- #

    async def register_tool(
        self,
        *,
        user_id: str | UUID,
        tool_name: str,
        tool_type: str,
        description: str,
        endpoint: str = "",
        method: str = "GET",
        parameters_schema: dict | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Register (or update) a tool in the vector store.

        If a tool with the same ``tool_name`` + ``user_id`` already exists it
        is deleted first (upsert semantic).
        """
        uid = str(user_id)
        collection = self._ensure_collection()

        # Remove old entry if exists (upsert)
        self.delete_tool(user_id=uid, tool_name=tool_name)

        # Build embedding from description + name for better retrieval
        embed_text = f"{tool_name}: {description}"
        vector = await self._embed(embed_text)

        param_schema = parameters_schema or {}
        meta = metadata or {}

        collection.insert([
            [vector],               # vector
            [tool_name],            # tool_name
            [uid],                  # user_id
            [tool_type],            # tool_type
            [description[:65000]],  # description
            [endpoint[:2048]],      # endpoint
            [method[:10]],          # method
            [param_schema],         # param_schema
            [meta],                 # metadata
        ])
        collection.flush()
        logger.info("Registered tool vector: %s (user=%s, type=%s)", tool_name, uid, tool_type)

    # -------------------------------------------------------------- #
    # Semantic search
    # -------------------------------------------------------------- #

    async def get_relevant_tools(
        self,
        user_query: str,
        user_id: str | UUID,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Search Milvus for the most relevant tools given a user query.

        Returns up to ``top_k`` tool descriptors sorted by relevance
        (cosine similarity), filtered to only this user's tools.
        """
        uid = str(user_id)
        collection = self._ensure_collection()

        query_vector = await self._embed(user_query)

        safe_uid = uid.replace("\\", "\\\\").replace('"', '\\"')

        search_kwargs = {
            "data": [query_vector],
            "anns_field": "vector",
            "param": {"metric_type": "COSINE", "params": {"ef": 64}},
            "limit": max(1, min(top_k, 50)),
            "expr": f'user_id == "{safe_uid}"',
        }
        try:
            results = collection.search(
                output_fields=_FULL_OUTPUT_FIELDS,
                **search_kwargs,
            )
        except Exception as exc:
            if not self._is_output_field_compatibility_error(exc):
                raise
            logger.info(
                "Milvus search fallback: JSON output fields unsupported, retrying with basic fields"
            )
            try:
                results = collection.search(
                    output_fields=_BASIC_OUTPUT_FIELDS,
                    **search_kwargs,
                )
            except Exception as fallback_exc:
                if not self._is_output_field_compatibility_error(fallback_exc):
                    raise
                logger.info(
                    "Milvus search fallback: vector search unsupported, querying user tools without vectors"
                )
                return self._query_tools_without_vector_search(
                    collection=collection,
                    safe_uid=safe_uid,
                    user_query=user_query,
                    top_k=top_k,
                )

        items: list[dict[str, Any]] = []
        for hit in results[0]:
            entity = hit.entity
            items.append({
                "score": float(hit.distance),
                "tool_name": entity.get("tool_name"),
                "tool_type": entity.get("tool_type"),
                "description": entity.get("description"),
                "endpoint": entity.get("endpoint"),
                "method": entity.get("method"),
                "parameters_schema": entity.get("param_schema") if isinstance(entity.get("param_schema"), dict) else {},
                "metadata": entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {},
            })
        return items

    @staticmethod
    def _is_output_field_compatibility_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        return "unsupported field type" in text or "field type: 0" in text

    @classmethod
    def _query_tools_without_vector_search(
        cls,
        *,
        collection: Collection,
        safe_uid: str,
        user_query: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        rows = collection.query(
            expr=f'user_id == "{safe_uid}"',
            output_fields=_BASIC_OUTPUT_FIELDS,
            limit=max(1, min(max(top_k * 5, top_k), 200)),
        )
        scored = sorted(
            (cls._score_plain_tool_row(user_query=user_query, row=row) for row in (rows or [])),
            key=lambda item: item["score"],
            reverse=True,
        )
        positive = [item for item in scored if float(item.get("score") or 0.0) > 0.0]
        if positive:
            scored = positive
        return scored[: max(1, min(top_k, 50))]

    @staticmethod
    def _score_plain_tool_row(*, user_query: str, row: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(
            str(row.get(key) or "")
            for key in ("tool_name", "description", "endpoint", "method")
        ).lower()
        row_tokens = set(_TOKEN_RE.findall(text))
        query_tokens = set(_TOKEN_RE.findall(str(user_query or "").lower()))
        overlap = len(query_tokens & row_tokens)
        score = float(overlap) / float(max(1, len(query_tokens)))
        return {
            "score": score,
            "tool_name": row.get("tool_name"),
            "tool_type": row.get("tool_type"),
            "description": row.get("description"),
            "endpoint": row.get("endpoint"),
            "method": row.get("method"),
            "parameters_schema": {},
            "metadata": {},
        }

    # -------------------------------------------------------------- #
    # Deletion
    # -------------------------------------------------------------- #

    def delete_tool(self, *, user_id: str | UUID, tool_name: str) -> int:
        """Delete a specific tool vector by user_id + tool_name."""
        uid = str(user_id).replace("\\", "\\\\").replace('"', '\\"')
        name = str(tool_name).replace("\\", "\\\\").replace('"', '\\"')

        collection = self._ensure_collection()
        result = collection.delete(
            expr=f'user_id == "{uid}" and tool_name == "{name}"'
        )
        collection.flush()
        count = int(getattr(result, "delete_count", 0) or 0)
        if count:
            logger.info("Deleted %d tool vector(s): %s (user=%s)", count, tool_name, uid)
        return count

    def delete_user_tools(self, user_id: str | UUID) -> int:
        """Delete ALL tool vectors for a given user."""
        uid = str(user_id).replace("\\", "\\\\").replace('"', '\\"')
        collection = self._ensure_collection()
        result = collection.delete(expr=f'user_id == "{uid}"')
        collection.flush()
        count = int(getattr(result, "delete_count", 0) or 0)
        logger.info("Deleted %d tool vectors for user %s", count, uid)
        return count

    def list_user_tools(self, user_id: str | UUID, limit: int = 100) -> list[dict[str, Any]]:
        """List all tool vectors registered for a user."""
        uid = str(user_id).replace("\\", "\\\\").replace('"', '\\"')
        collection = self._ensure_collection()
        rows = collection.query(
            expr=f'user_id == "{uid}"',
            output_fields=[
                "tool_name", "tool_type", "description",
                "endpoint", "method", "param_schema", "metadata",
            ],
            limit=max(1, min(limit, 1000)),
        )
        return [
            {
                "tool_name": row.get("tool_name"),
                "tool_type": row.get("tool_type"),
                "description": row.get("description"),
                "endpoint": row.get("endpoint"),
                "method": row.get("method"),
                "parameters_schema": row.get("param_schema"),
                "metadata": row.get("metadata"),
            }
            for row in (rows or [])
        ]


# Singleton
vector_tool_registry = VectorToolRegistry()
