from typing import Annotated
import logging

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.api.types import CurrentUser
from app.core.config import settings
from app.schemas.documents import DocumentDeleteResponse, DocumentListResponse
from app.services.rag_service import rag_service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post(
    "/upload",
    responses={
        400: {"description": "Unsupported file format"},
        503: {"description": "Embedding service unavailable"},
    },
)
async def upload_document(
    file: Annotated[UploadFile, File(...)],
    current_user: CurrentUser,
) -> dict:
    try:
        if settings.DEV_VERBOSE_LOGGING:
            logger.info(
                "documents upload start",
                extra={
                    "context": {
                        "component": "documents",
                        "event": "upload_start",
                        "user_id": str(current_user.id),
                        "filename": file.filename or "document",
                    }
                },
            )
        content = await file.read()
        chunks = await rag_service.ingest_document(str(current_user.id), file.filename or "document", content)
        if settings.DEV_VERBOSE_LOGGING:
            logger.info(
                "documents upload done",
                extra={
                    "context": {
                        "component": "documents",
                        "event": "upload_done",
                        "user_id": str(current_user.id),
                        "chunks": chunks,
                    }
                },
            )
        return {"status": "ok", "chunks": chunks}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/search", responses={503: {"description": "Embedding service unavailable"}})
async def search_document(query: str, current_user: CurrentUser, top_k: int = 5) -> dict:
    try:
        items = await rag_service.retrieve_context(str(current_user.id), query, top_k=top_k)
        if settings.DEV_VERBOSE_LOGGING:
            logger.info(
                "documents search",
                extra={
                    "context": {
                        "component": "documents",
                        "event": "search",
                        "user_id": str(current_user.id),
                        "query": query,
                        "top_k": top_k,
                        "items_count": len(items),
                    }
                },
            )
        return {"items": items}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("", responses={503: {"description": "Document list unavailable"}})
async def list_documents(current_user: CurrentUser, limit: int = 200) -> DocumentListResponse:
    try:
        items = await rag_service.list_documents(str(current_user.id), limit=max(1, min(limit, 1000)))
        if settings.DEV_VERBOSE_LOGGING:
            logger.info(
                "documents list",
                extra={
                    "context": {
                        "component": "documents",
                        "event": "list",
                        "user_id": str(current_user.id),
                        "limit": limit,
                        "items_count": len(items),
                    }
                },
            )
        return DocumentListResponse(items=items)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete("/all", responses={503: {"description": "Document delete unavailable"}})
async def delete_all_documents(current_user: CurrentUser) -> DocumentDeleteResponse:
    try:
        deleted_count = await rag_service.delete_all_documents(str(current_user.id))
        if settings.DEV_VERBOSE_LOGGING:
            logger.info(
                "documents delete all",
                extra={
                    "context": {
                        "component": "documents",
                        "event": "delete_all",
                        "user_id": str(current_user.id),
                        "deleted_count": deleted_count,
                    }
                },
            )
        return DocumentDeleteResponse(status="deleted_all", deleted_count=deleted_count)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete(
    "/{source_doc}",
    responses={
        400: {"description": "Invalid source_doc"},
        503: {"description": "Document delete unavailable"},
    },
)
async def delete_document(source_doc: str, current_user: CurrentUser) -> DocumentDeleteResponse:
    normalized = str(source_doc or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="source_doc is required")
    try:
        deleted_count = await rag_service.delete_document(str(current_user.id), normalized)
        if settings.DEV_VERBOSE_LOGGING:
            logger.info(
                "documents delete",
                extra={
                    "context": {
                        "component": "documents",
                        "event": "delete",
                        "user_id": str(current_user.id),
                        "source_doc": normalized,
                        "deleted_count": deleted_count,
                    }
                },
            )
        if deleted_count <= 0:
            return DocumentDeleteResponse(status="not_found", deleted_count=0, source_doc=normalized)
        return DocumentDeleteResponse(status="deleted", deleted_count=deleted_count, source_doc=normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
