from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.api.types import CurrentUser
from app.services.rag_service import rag_service

router = APIRouter()


@router.post("/upload", responses={400: {"description": "Unsupported file format"}})
async def upload_document(
    file: Annotated[UploadFile, File(...)],
    current_user: CurrentUser,
) -> dict:
    try:
        content = await file.read()
        chunks = await rag_service.ingest_document(str(current_user.id), file.filename or "document", content)
        return {"status": "ok", "chunks": chunks}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/search")
async def search_document(query: str, current_user: CurrentUser, top_k: int = 5) -> dict:
    try:
        items = await rag_service.retrieve_context(str(current_user.id), query, top_k=top_k)
        return {"items": items}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
