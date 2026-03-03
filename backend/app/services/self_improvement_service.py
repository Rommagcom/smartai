import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message
from app.models.user import User

logger = logging.getLogger(__name__)


class SelfImprovementService:
    async def analyze_feedback(self, db: AsyncSession, user_id: str) -> dict:
        result = await db.execute(
            select(Message)
            .where(Message.user_id == user_id, Message.feedback_score.is_not(None))
            .order_by(Message.created_at.desc())
            .limit(100)
        )
        messages = result.scalars().all()
        if not messages:
            return {"negative_ratio": 0.0, "samples": 0}

        negatives = sum(1 for msg in messages if (msg.feedback_score or 0) < 0)
        return {"negative_ratio": negatives / len(messages), "samples": len(messages)}

    async def adapt_preferences(self, db: AsyncSession, user: User) -> dict:
        analysis = await self.analyze_feedback(db, str(user.id))
        prefs = dict(user.preferences or {})
        changed = False

        if analysis["samples"] >= 5:
            ratio = analysis["negative_ratio"]
            if ratio > 0.5:
                new_temp = 0.2
                new_style = "concise"
            elif ratio > 0.25:
                new_temp = 0.3
                new_style = "balanced"
            else:
                new_temp = max(0.3, float(prefs.get("temperature", 0.4)))
                new_style = prefs.get("style", "balanced")

            if prefs.get("temperature") != new_temp:
                prefs["temperature"] = new_temp
                changed = True
            if prefs.get("adapted_style") != new_style:
                prefs["adapted_style"] = new_style
                changed = True

            if changed:
                user.preferences = prefs
                db.add(user)
                await db.commit()
                await db.refresh(user)
                logger.info(
                    "adapted preferences for user %s: temperature=%.1f, style=%s (negative_ratio=%.2f, samples=%d)",
                    user.id, new_temp, new_style, ratio, analysis["samples"],
                )

        return {"analysis": analysis, "preferences": user.preferences, "adapted": changed}

    async def maybe_auto_adapt(self, db: AsyncSession, user: User) -> None:
        """Auto-adapt after every 5th feedback."""
        analysis = await self.analyze_feedback(db, str(user.id))
        if analysis["samples"] >= 5 and analysis["samples"] % 5 == 0:
            await self.adapt_preferences(db, user)


self_improvement_service = SelfImprovementService()
