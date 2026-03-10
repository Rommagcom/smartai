from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.types import CurrentUser, DBSession
from app.models.cron_job import CronJob
from app.models.user import User
from app.schemas.cron import CronJobCreate, CronJobOut
from app.services.scheduler_service import scheduler_service

router = APIRouter()


@router.post("", response_model=CronJobOut)
async def create_cron_job(
    payload: CronJobCreate,
    db: DBSession,
    current_user: CurrentUser,
) -> CronJob:
    normalized_action_type = str(payload.action_type or "send_message").strip().lower()
    if normalized_action_type in {"reminder", "notification", "daily_briefing"}:
        normalized_action_type = "send_message"

    cron = CronJob(
        user_id=current_user.id,
        name=payload.name,
        cron_expression=payload.cron_expression,
        action_type=normalized_action_type,
        payload=payload.payload,
        is_active=payload.is_active,
    )
    db.add(cron)
    await db.commit()
    await db.refresh(cron)

    if scheduler_service.scheduler.running:
        scheduler_service.add_or_replace_job(
            job_id=str(cron.id),
            cron_expression=cron.cron_expression,
            user_id=str(current_user.id),
            action_type=cron.action_type,
            payload=cron.payload,
        )
    return cron


@router.get("", response_model=list[CronJobOut])
async def list_cron_jobs(
    db: DBSession,
    current_user: CurrentUser,
) -> list[CronJob]:
    result = await db.execute(
        select(CronJob)
        .where(CronJob.user_id == current_user.id)
        .order_by(CronJob.created_at.desc())
    )
    return result.scalars().all()


@router.delete("/{job_id}", responses={404: {"description": "Cron job not found"}})
async def delete_cron_job(
    job_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    result = await db.execute(select(CronJob).where(CronJob.id == job_id, CronJob.user_id == current_user.id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Cron job not found")

    if scheduler_service.scheduler.running and scheduler_service.scheduler.get_job(str(job.id)):
        scheduler_service.scheduler.remove_job(str(job.id))

    await db.delete(job)
    await db.commit()
    return {"status": "deleted"}
