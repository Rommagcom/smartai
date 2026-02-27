from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility

from app.core.config import settings


class MilvusService:
    def __init__(self) -> None:
        self.collection_name = settings.MILVUS_COLLECTION

    def connect(self) -> None:
        connections.connect(alias="default", host=settings.MILVUS_HOST, port=str(settings.MILVUS_PORT))

    def ensure_collection(self) -> Collection:
        self.connect()
        if utility.has_collection(self.collection_name):
            return Collection(self.collection_name)

        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="user_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="chunk_text", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=settings.EMBEDDING_DIM),
            FieldSchema(name="source_doc", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="metadata", dtype=DataType.JSON),
        ]
        schema = CollectionSchema(fields=fields, description="User knowledge base")
        collection = Collection(self.collection_name, schema=schema)
        collection.create_index(field_name="embedding", index_params={"index_type": "HNSW", "metric_type": "COSINE", "params": {"M": 16, "efConstruction": 200}})
        collection.load()
        return collection

    def insert_chunks(self, user_id: str, chunks: list[str], vectors: list[list[float]], source_doc: str) -> None:
        collection = self.ensure_collection()
        metadata = [{"source": source_doc, "user_id": user_id} for _ in chunks]
        collection.insert([ [user_id for _ in chunks], chunks, vectors, [source_doc for _ in chunks], metadata ])
        collection.flush()

    def search(self, user_id: str, query_embedding: list[float], top_k: int = 5) -> list[dict]:
        collection = self.ensure_collection()
        collection.load()
        results = collection.search(
            data=[query_embedding],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"ef": 64}},
            limit=top_k,
            output_fields=["user_id", "chunk_text", "source_doc"],
            expr=f'user_id == "{user_id}"',
        )
        items: list[dict] = []
        for hit in results[0]:
            entity = hit.entity
            items.append(
                {
                    "score": float(hit.distance),
                    "chunk_text": entity.get("chunk_text"),
                    "source_doc": entity.get("source_doc"),
                    "metadata": None,
                }
            )
        return items

    def delete_user_chunks(self, user_id: str) -> int:
        collection = self.ensure_collection()
        result = collection.delete(expr=f'user_id == "{user_id}"')
        collection.flush()
        return int(getattr(result, "delete_count", 0) or 0)


milvus_service = MilvusService()
