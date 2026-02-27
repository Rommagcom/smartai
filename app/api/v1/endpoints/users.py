from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.api.types import CurrentUser, DBSession
from app.models.user import User
from app.schemas.soul import SoulAdaptTaskRequest, SoulOnboardingStep, SoulSetupRequest, SoulSetupResponse, SoulStatus
from app.schemas.user import UserAdminAccessUpdate, UserOut, UserPreferencesUpdate
from app.services.milvus_service import milvus_service
from app.services.scheduler_service import scheduler_service
from app.services.soul_service import soul_service
from app.services.worker_result_service import worker_result_service

router = APIRouter()


def _require_admin(current_user: User) -> None:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")


def _to_user_out(current_user: User) -> UserOut:
    requires_soul_setup = not current_user.soul_configured
    onboarding = soul_service.get_onboarding_payload(current_user) if requires_soul_setup else None
    return UserOut(
        id=current_user.id,
        username=current_user.username,
        is_admin=current_user.is_admin,
        preferences=current_user.preferences,
        soul_profile=current_user.soul_profile,
        soul_configured=current_user.soul_configured,
        requires_soul_setup=requires_soul_setup,
        soul_onboarding=onboarding,
        system_prompt_template=current_user.system_prompt_template,
        created_at=current_user.created_at,
    )


@router.get("/me", response_model=UserOut)
async def get_me(current_user: CurrentUser) -> UserOut:
    return _to_user_out(current_user)


@router.patch("/me/preferences", response_model=UserOut)
async def update_preferences(
    payload: UserPreferencesUpdate,
    db: DBSession,
    current_user: CurrentUser,
) -> UserOut:
    current_user.preferences = payload.preferences
    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)
    return _to_user_out(current_user)


@router.get("/admin/users", response_model=list[UserOut])
async def admin_list_users(db: DBSession, current_user: CurrentUser) -> list[UserOut]:
    _require_admin(current_user)
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    return [_to_user_out(user) for user in users]


@router.patch("/admin/users/{user_id}/admin-access", response_model=UserOut)
async def admin_set_user_admin_access(
    user_id: UUID,
    payload: UserAdminAccessUpdate,
    db: DBSession,
    current_user: CurrentUser,
) -> UserOut:
    _require_admin(current_user)

    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if target_user.is_admin and not payload.is_admin:
        admin_count_result = await db.execute(select(func.count()).select_from(User).where(User.is_admin.is_(True)))
        admin_count = int(admin_count_result.scalar() or 0)
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot revoke admin access from the last admin",
            )

    target_user.is_admin = payload.is_admin
    db.add(target_user)
    await db.commit()
    await db.refresh(target_user)
    return _to_user_out(target_user)


@router.delete("/admin/users/{user_id}", responses={404: {"description": "User not found"}})
async def admin_delete_user(
    user_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    _require_admin(current_user)

    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if target_user.is_admin:
        admin_count_result = await db.execute(select(func.count()).select_from(User).where(User.is_admin.is_(True)))
        admin_count = int(admin_count_result.scalar() or 0)
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete the last admin",
            )

    user_id_str = str(target_user.id)
    scheduler_jobs_removed = scheduler_service.remove_jobs_for_user(user_id=user_id_str)

    try:
        milvus_deleted_chunks = milvus_service.delete_user_chunks(user_id=user_id_str)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"User deletion blocked: document storage unavailable ({exc})",
        ) from exc

    await worker_result_service.clear_user_results(user_id=user_id_str)

    await db.delete(target_user)
    await db.commit()

    return {
        "status": "deleted",
        "user_id": user_id_str,
        "cleanup": {
            "scheduler_jobs_removed": scheduler_jobs_removed,
            "milvus_chunks_deleted": milvus_deleted_chunks,
            "worker_results_cleared": True,
        },
    }


@router.get("/me/soul/status", response_model=SoulStatus)
async def soul_status(current_user: CurrentUser) -> SoulStatus:
    return SoulStatus(**soul_service.get_status(current_user))


@router.get("/me/onboarding-next-step", response_model=SoulOnboardingStep)
async def soul_onboarding_next_step(current_user: CurrentUser) -> SoulOnboardingStep:
    return SoulOnboardingStep(**soul_service.get_next_onboarding_step(current_user))


@router.post("/me/soul/setup", response_model=SoulSetupResponse)
async def soul_setup(
    payload: SoulSetupRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> SoulSetupResponse:
    soul_service.setup_user_soul(
        user=current_user,
        user_description=payload.user_description,
        assistant_name=payload.assistant_name,
        emoji=payload.emoji,
        style=payload.style,
        tone_modifier=payload.tone_modifier,
        task_mode=payload.task_mode,
    )
    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)

    profile = current_user.soul_profile or {}
    return SoulSetupResponse(
        configured=current_user.soul_configured,
        assistant_name=profile.get("assistant_name", "SOUL"),
        emoji=profile.get("emoji", "🧠"),
        style=profile.get("style", "direct"),
        task_mode=profile.get("task_mode", "other"),
        first_question="Кто ты и чем занимаемся?",
        system_prompt_template=current_user.system_prompt_template,
    )


@router.post("/me/soul/adapt-task", response_model=SoulSetupResponse)
async def soul_adapt_task(
    payload: SoulAdaptTaskRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> SoulSetupResponse:
    soul_service.adapt_task(current_user, task_mode=payload.task_mode, custom_task=payload.custom_task)
    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)

    profile = current_user.soul_profile or {}
    return SoulSetupResponse(
        configured=current_user.soul_configured,
        assistant_name=profile.get("assistant_name", "SOUL"),
        emoji=profile.get("emoji", "🧠"),
        style=profile.get("style", "direct"),
        task_mode=profile.get("task_mode", "other"),
        first_question="Кто ты и чем занимаемся?",
        system_prompt_template=current_user.system_prompt_template,
    )
