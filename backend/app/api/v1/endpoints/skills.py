from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.api.types import AdminUser, CurrentUser, DBSession
from app.schemas.skills import SkillsRegistryResponse
from app.services.dynamic_tool_service import dynamic_tool_service
from app.services.tool_catalog_service import tool_catalog_service

router = APIRouter()


@router.get("/registry")
async def skills_registry(
    current_user: CurrentUser,
) -> SkillsRegistryResponse:
    del current_user
    return SkillsRegistryResponse(
        registry_version=tool_catalog_service.REGISTRY_VERSION,
        skills=tool_catalog_service.list_skill_contracts(),
        integration_tools=tool_catalog_service.list_integration_contracts(),
        dynamic_tool_operations=tool_catalog_service.list_dynamic_tool_contracts(),
    )


@router.post(
    "/upload",
    responses={
        400: {"description": "Invalid skill package"},
        403: {"description": "Only administrators can upload Python Skills"},
    },
)
async def upload_dynamic_skill(
    file: Annotated[UploadFile, File(...)],
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    if not bool(current_user.is_admin):
        raise HTTPException(status_code=403, detail="Only administrators can upload Python Skills")

    filename = str(file.filename or "add_skill.zip").strip() or "add_skill.zip"
    content = await file.read()
    result = await dynamic_tool_service.register_skill_package(
        db=db,
        user_id=current_user.id,
        filename=filename,
        content=content,
    )
    if str(result.get("status") or "") == "failed":
        raise HTTPException(status_code=400, detail=str(result.get("message") or "invalid skill package"))
    return result


@router.get("")
async def list_dynamic_skills(
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    tools = await dynamic_tool_service.list_tools(
        db=db,
        user_id=current_user.id,
        active_only=True,
        kind="python_skill",
    )
    items = [
        {
            "id": str(tool.id),
            "name": tool.name,
            "kind": "python_skill",
            "description": tool.description,
            "method": tool.method,
            "endpoint": tool.endpoint,
            "created_at": tool.created_at,
            "updated_at": getattr(tool, "updated_at", tool.created_at),
        }
        for tool in tools
    ]
    return {"items": items, "count": len(items)}


@router.delete(
    "/{skill_name}",
    responses={
        403: {"description": "Only administrators can delete Python Skills"},
        404: {"description": "Python skill not found"},
    },
)
async def delete_dynamic_skill(
    skill_name: str,
    db: DBSession,
    current_user: AdminUser,
) -> dict:
    deleted = await dynamic_tool_service.delete_tool_by_name(
        db=db,
        user_id=current_user.id,
        tool_name=skill_name,
        kind="python_skill",
    )
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Python Skill '{skill_name}' not found")
    return {"status": "ok", "deleted": 1, "tool_name": str(skill_name)}


@router.delete(
    "",
    responses={403: {"description": "Only administrators can delete Python Skills"}},
)
async def delete_all_dynamic_skills(
    db: DBSession,
    current_user: AdminUser,
) -> dict:
    deleted_count = await dynamic_tool_service.delete_all_tools(
        db=db,
        user_id=current_user.id,
        kind="python_skill",
    )
    return {"status": "ok", "deleted_count": int(deleted_count)}