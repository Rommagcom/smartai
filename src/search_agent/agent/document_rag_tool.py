from __future__ import annotations

import base64
from io import BytesIO
import json
import os
import re
from typing import Any

from langchain.chains import RetrievalQA
from langchain_core.documents import Document
from langchain_milvus import Milvus
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader


_DEFAULT_EMBEDDING_MODEL = "nomic-embed-text:latest"
_DEFAULT_LLM_MODEL = os.getenv("OLLAMA_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
_DEFAULT_LLM_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.3").strip() or "0.3")
_DEFAULT_SEARCH_TYPE = "mmr"
_ALLOWED_SEARCH_TYPES = {"mmr", "similarity", "similarity_score_threshold"}
_ALLOWED_SCOPE = {"team", "private"}
_SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf"}
_DEFAULT_MILVUS_HOST = os.getenv("MILVUS_HOST", "127.0.0.1").strip() or "127.0.0.1"
_DEFAULT_MILVUS_PORT = int(os.getenv("MILVUS_PORT", "19530").strip() or "19530")


DOCUMENT_RAG_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "document_rag",
        "description": (
            "Built-in persistent document RAG tool. "
            "Index uploaded document content (base64) into Milvus and answer questions over indexed data. "
            "Supports team-shared and private scopes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action to execute: index or query",
                    "enum": ["index", "query"],
                },
                "query": {
                    "type": "string",
                    "description": "Question for action=query",
                },
                "file_name": {
                    "type": "string",
                    "description": "Uploaded file name (.txt, .md, .pdf) for action=index",
                },
                "file_content_base64": {
                    "type": "string",
                    "description": "Uploaded file content in base64 for action=index",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() or default


def _slug(value: str, default: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", value.lower()).strip("_")
    return cleaned or default


def _resolve_scope(scope: str) -> str:
    normalized = _text(scope, "team").lower()
    if normalized not in _ALLOWED_SCOPE:
        allowed = ", ".join(sorted(_ALLOWED_SCOPE))
        raise ValueError(f"scope must be one of: {allowed}")
    return normalized


def _resolve_collection_name(
    *,
    scope: str,
    org_id: str,
    team_id: str,
    user_id: int,
    collection_name: str | None,
) -> str:
    explicit = _text(collection_name)
    if explicit:
        return _slug(explicit, "rag_docs")

    org_part = _slug(org_id, "default_org")
    team_part = _slug(team_id, "chat")
    if scope == "private":
        return f"rag_docs_private_{org_part}_{team_part}_u{int(user_id)}"
    return f"rag_docs_team_{org_part}_{team_part}"


def _load_uploaded_documents(*, file_name: str, file_content_base64: str) -> list[Document]:
    source_name = _text(file_name)
    if not source_name:
        raise ValueError("file_name is required for action=index")

    suffix = "." + source_name.rsplit(".", 1)[-1].lower() if "." in source_name else ""
    if suffix not in _SUPPORTED_SUFFIXES:
        allowed = ", ".join(sorted(_SUPPORTED_SUFFIXES))
        raise ValueError(f"Unsupported file extension: {suffix or '<none>'}. Allowed: {allowed}")

    try:
        raw_bytes = base64.b64decode(_text(file_content_base64), validate=True)
    except Exception as exc:
        raise ValueError("file_content_base64 must be valid base64") from exc

    if not raw_bytes:
        raise ValueError("Uploaded file is empty")

    if suffix in {".txt", ".md"}:
        content = raw_bytes.decode("utf-8", errors="replace")
        return [Document(page_content=content, metadata={"source": source_name})]

    reader = PdfReader(BytesIO(raw_bytes))
    docs: list[Document] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = str(page.extract_text() or "").strip()
        if not text:
            continue
        docs.append(
            Document(
                page_content=text,
                metadata={
                    "source": source_name,
                    "page": page_number,
                },
            )
        )
    if not docs:
        raise ValueError("Uploaded PDF has no extractable text")
    return docs


def chunk_documents(docs: list[Document], chunk_size: int = 1000, overlap: int = 200) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=int(chunk_size), chunk_overlap=int(overlap))
    return splitter.split_documents(docs)


def _build_embeddings(model_name: str) -> OllamaEmbeddings:
    return OllamaEmbeddings(model=model_name)


def _build_connection_args(host: str, port: int) -> dict[str, object]:
    return {
        "host": _text(host, _DEFAULT_MILVUS_HOST),
        "port": int(port),
    }


def _index_documents(
    *,
    file_name: str,
    file_content_base64: str,
    chunk_size: int,
    overlap: int,
    drop_old: bool,
    collection_name: str,
    embedding_model: str,
    host: str,
    port: int,
    scope: str,
    org_id: str,
    team_id: str,
    user_id: int,
) -> str:
    docs = _load_uploaded_documents(file_name=file_name, file_content_base64=file_content_base64)

    for doc in docs:
        doc.metadata = dict(doc.metadata or {})
        doc.metadata["org_id"] = org_id
        doc.metadata["team_id"] = team_id
        doc.metadata["user_id"] = int(user_id)
        doc.metadata["scope"] = scope

    chunks = chunk_documents(docs, chunk_size=chunk_size, overlap=overlap)
    embeddings = _build_embeddings(embedding_model)

    vectorstore = Milvus.from_documents(
        documents=chunks,
        embedding=embeddings,
        collection_name=collection_name,
        connection_args=_build_connection_args(host, port),
        index_params={"index_type": "HNSW", "metric_type": "L2", "nlist": 128},
        drop_old=bool(drop_old),
    )

    payload = {
        "status": "indexed",
        "collection_name": collection_name,
        "scope": scope,
        "documents_count": len(docs),
        "chunks_count": len(chunks),
        "embedding_model": embedding_model,
        "milvus": _build_connection_args(host, port),
        "source": _text(file_name),
        "loaded_sources": sorted({str(doc.metadata.get("source") or "") for doc in docs}),
        "index_type": "HNSW",
        "metric_type": "L2",
        "drop_old": bool(drop_old),
        "collection_description": str(vectorstore.col.name) if getattr(vectorstore, "col", None) is not None else "",
    }
    return json.dumps(payload, ensure_ascii=True)


def _query_documents(
    *,
    query: str,
    collection_name: str,
    embedding_model: str,
    search_type: str,
    top_k: int,
    host: str,
    port: int,
    return_source_documents: bool,
) -> str:
    if not _text(query):
        raise ValueError("query is required for action=query")

    normalized_search = _text(search_type, _DEFAULT_SEARCH_TYPE).lower()
    if normalized_search not in _ALLOWED_SEARCH_TYPES:
        allowed = ", ".join(sorted(_ALLOWED_SEARCH_TYPES))
        raise ValueError(f"search_type must be one of: {allowed}")

    embeddings = _build_embeddings(embedding_model)
    vectorstore = Milvus(
        embedding_function=embeddings,
        collection_name=collection_name,
        connection_args=_build_connection_args(host, port),
    )

    retriever = vectorstore.as_retriever(
        search_type=normalized_search,
        search_kwargs={"k": int(top_k)},
    )
    llm = ChatOllama(model=_DEFAULT_LLM_MODEL, temperature=float(_DEFAULT_LLM_TEMPERATURE))

    chain = RetrievalQA.from_chain_type(
        llm=llm,
        chain_type="stuff",
        retriever=retriever,
        return_source_documents=bool(return_source_documents),
    )
    result = chain.invoke({"query": query})

    payload: dict[str, Any] = {
        "status": "ok",
        "collection_name": collection_name,
        "query": query,
        "search_type": normalized_search,
        "top_k": int(top_k),
        "answer": str(result.get("result") or ""),
    }

    if return_source_documents:
        sources: list[dict[str, Any]] = []
        for doc in result.get("source_documents") or []:
            if not isinstance(doc, Document):
                continue
            sources.append(
                {
                    "source": str((doc.metadata or {}).get("source") or ""),
                    "metadata": doc.metadata or {},
                    "preview": str(doc.page_content or "")[:400],
                }
            )
        payload["sources"] = sources
        payload["sources_count"] = len(sources)

    return json.dumps(payload, ensure_ascii=True)


def document_rag(
    action: str,
    query: str | None = None,
    file_name: str | None = None,
    file_content_base64: str | None = None,
    chunk_size: int = 768,
    overlap: int = 200,
    collection_name: str | None = None,
    drop_old: bool = False,
    search_type: str = _DEFAULT_SEARCH_TYPE,
    top_k: int = 5,
    embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
    return_source_documents: bool = True,
    scope: str = "team",
    org_id: str = "default-org",
    team_id: str = "chat",
    user_id: int = 0,
    milvus_host: str = _DEFAULT_MILVUS_HOST,
    milvus_port: int = _DEFAULT_MILVUS_PORT,
) -> str:
    normalized_action = _text(action).lower()
    if normalized_action not in {"index", "query"}:
        raise ValueError("action must be one of: index, query")

    resolved_scope = _resolve_scope(scope)
    resolved_org_id = _text(org_id, "default-org")
    resolved_team_id = _text(team_id, "chat")
    resolved_user_id = int(user_id)
    resolved_collection_name = _resolve_collection_name(
        scope=resolved_scope,
        org_id=resolved_org_id,
        team_id=resolved_team_id,
        user_id=resolved_user_id,
        collection_name=collection_name,
    )

    if int(chunk_size) <= 0:
        raise ValueError("chunk_size must be greater than 0")
    if int(overlap) < 0:
        raise ValueError("overlap must be >= 0")
    if int(overlap) >= int(chunk_size):
        raise ValueError("overlap must be less than chunk_size")
    if int(top_k) <= 0:
        raise ValueError("top_k must be greater than 0")

    if normalized_action == "index":
        if not _text(file_name):
            raise ValueError("file_name is required for action=index")
        if not _text(file_content_base64):
            raise ValueError("file_content_base64 is required for action=index")
        return _index_documents(
            file_name=_text(file_name),
            file_content_base64=_text(file_content_base64),
            chunk_size=int(chunk_size),
            overlap=int(overlap),
            drop_old=bool(drop_old),
            collection_name=resolved_collection_name,
            embedding_model=_text(embedding_model, _DEFAULT_EMBEDDING_MODEL),
            host=milvus_host,
            port=int(milvus_port),
            scope=resolved_scope,
            org_id=resolved_org_id,
            team_id=resolved_team_id,
            user_id=resolved_user_id,
        )

    return _query_documents(
        query=_text(query),
        collection_name=resolved_collection_name,
        embedding_model=_text(embedding_model, _DEFAULT_EMBEDDING_MODEL),
        search_type=search_type,
        top_k=int(top_k),
        host=milvus_host,
        port=int(milvus_port),
        return_source_documents=bool(return_source_documents),
    )
