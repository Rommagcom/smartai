from pydantic import BaseModel

from app.api.types import AdminUser, CurrentUser, DBSession
from app.schemas.dynamic_tool import DynamicToolOut
from app.services.dynamic_tool_service import dynamic_tool_service

from fastapi import APIRouter

router = APIRouter()


class ApiToolRegisterRequest(BaseModel):
    user_message: str


@router.get("")
async def list_api_tools(
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    tools = await dynamic_tool_service.list_tools(
        db=db,
        user_id=current_user.id,
        active_only=True,
        kind="api_tool",
    )
    items = [DynamicToolOut.model_validate(tool).model_dump(mode="json") for tool in tools]
    return {"items": items, "count": len(items)}


@router.post("/register")
async def register_api_tool(
    payload: ApiToolRegisterRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    return await dynamic_tool_service.register_from_user_message(
        db=db,
        user_id=current_user.id,
        user_message=payload.user_message,
    )


@router.delete("/{tool_name}")
async def delete_api_tool(
    tool_name: str,
    db: DBSession,
    current_user: AdminUser,
) -> dict:
    deleted = await dynamic_tool_service.delete_tool_by_name(
        db=db,
        user_id=current_user.id,
        tool_name=tool_name,
        kind="api_tool",
    )
    if not deleted:
        return {"status": "not_found", "deleted": 0, "tool_name": tool_name}
    return {"status": "ok", "deleted": 1, "tool_name": tool_name}


@router.delete("")
async def delete_all_api_tools(
    db: DBSession,
    current_user: AdminUser,
) -> dict:
    deleted_count = await dynamic_tool_service.delete_all_tools(
        db=db,
        user_id=current_user.id,
        kind="api_tool",
    )
    return {"status": "ok", "deleted_count": int(deleted_count)}