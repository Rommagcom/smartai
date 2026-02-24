from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.types import CurrentUser, DBSession
from app.models.code_snippet import CodeSnippet
from app.models.message import Message
from app.models.user import User
from app.schemas.chat import (
    BrowserActionRequest,
    ChatRequest,
    ChatResponse,
    FeedbackRequest,
    MessageOut,
    PdfCreateRequest,
    WebFetchRequest,
    WebSearchRequest,
)
from app.services.chat_service import chat_service
from app.services.memory_service import memory_service
from app.services.pdf_service import pdf_service
from app.services.sandbox_service import sandbox_service
from app.services.self_improvement_service import self_improvement_service
from app.services.web_tools_service import web_tools_service
from app.services.worker_result_service import worker_result_service

router = APIRouter()


@router.post("")
async def chat(
    payload: ChatRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> ChatResponse:
    if not current_user.soul_configured:
        raise HTTPException(
            status_code=428,
            detail={
                "message": "SOUL initial setup is required before first chat",
                "setup_endpoint": "/api/v1/users/me/soul/setup",
                "status_endpoint": "/api/v1/users/me/soul/status",
                "first_question": "Кто ты и чем занимаемся?",
            },
        )

    session = await memory_service.get_or_create_session(db, current_user.id, payload.session_id)
    await memory_service.append_message(db, current_user.id, session.id, "user", payload.message)

    response_text, used_memory_ids, rag_sources, tool_calls, artifacts = await chat_service.respond(
        db,
        current_user,
        session.id,
        payload.message,
    )
    await memory_service.append_message(
        db,
        current_user.id,
        session.id,
        "assistant",
        response_text,
        message_meta={
            "used_memory_ids": used_memory_ids,
            "rag_sources": rag_sources,
            "tool_calls": tool_calls,
        },
    )
    await memory_service.extract_and_store_facts(db, current_user.id, payload.message, response_text)

    await db.commit()
    return ChatResponse(
        session_id=session.id,
        response=response_text,
        used_memory_ids=[UUID(mid) for mid in used_memory_ids],
        tool_calls=tool_calls,
        artifacts=artifacts,
    )


@router.get("/history/{session_id}", response_model=list[MessageOut])
async def chat_history(
    session_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.user_id == current_user.id, Message.session_id == session_id)
        .order_by(Message.created_at.asc())
    )
    return result.scalars().all()


@router.post("/feedback", responses={404: {"description": "Message not found"}})
async def set_feedback(
    payload: FeedbackRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    result = await db.execute(select(Message).where(Message.id == payload.message_id, Message.user_id == current_user.id))
    message = result.scalar_one_or_none()
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    message.feedback_score = payload.feedback_score
    db.add(message)
    await db.commit()

    analysis = await self_improvement_service.analyze_feedback(db, str(current_user.id))
    return {"status": "ok", "analysis": analysis}


@router.post("/self-improve")
async def self_improve(
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    return await self_improvement_service.adapt_preferences(db, current_user)


@router.post("/execute-python", responses={400: {"description": "code is required"}})
async def execute_python(
    body: dict,
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    code = body.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="code is required")

    result = await sandbox_service.execute_python_code(code, current_user.id)
    snippet = CodeSnippet(
        user_id=current_user.id,
        code=code,
        language="python",
        execution_result=result,
        is_successful=result.get("success", False),
        created_by="assistant",
    )
    db.add(snippet)
    await db.commit()
    return result


@router.post("/tools/web-search")
async def web_search(
    payload: WebSearchRequest,
    current_user: CurrentUser,
) -> dict:
    del current_user
    limit = max(1, min(payload.limit, 10))
    return await web_tools_service.web_search(query=payload.query, limit=limit)


@router.post("/tools/web-fetch")
async def web_fetch(
    payload: WebFetchRequest,
    current_user: CurrentUser,
) -> dict:
    del current_user
    max_chars = max(1000, min(payload.max_chars, 50000))
    return await web_tools_service.web_fetch(url=payload.url, max_chars=max_chars)


@router.post("/tools/browser")
async def browser_action(
    payload: BrowserActionRequest,
    current_user: CurrentUser,
) -> dict:
    del current_user
    action = payload.action.strip().lower()
    if action not in {"extract_text", "screenshot", "pdf"}:
        raise HTTPException(status_code=400, detail="action must be one of: extract_text, screenshot, pdf")

    max_chars = max(1000, min(payload.max_chars, 50000))
    timeout_seconds = max(5, min(payload.timeout_seconds, 120))
    return await web_tools_service.browser_action(
        url=payload.url,
        action=action,
        max_chars=max_chars,
        timeout_seconds=timeout_seconds,
    )


@router.post("/tools/pdf-create")
async def pdf_create(
    payload: PdfCreateRequest,
    current_user: CurrentUser,
) -> dict:
    del current_user
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="content must not be empty")

    filename = payload.filename.strip() or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"

    return pdf_service.create_pdf_base64(
        title=payload.title.strip() or "Generated document",
        content=payload.content,
        filename=filename,
    )


@router.get("/worker-results/poll")
async def poll_worker_results(
    current_user: CurrentUser,
    limit: int = 20,
) -> dict:
    items = worker_result_service.pop_many(user_id=str(current_user.id), limit=limit)
    return {"items": items}
