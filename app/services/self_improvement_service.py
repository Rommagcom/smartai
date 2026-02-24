from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message
from app.models.user import User


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

        if analysis["samples"] >= 5:
            if analysis["negative_ratio"] > 0.5:
                prefs["temperature"] = 0.2
                prefs["style"] = "concise"
            else:
                prefs.setdefault("temperature", 0.4)
                prefs.setdefault("style", "balanced")

            user.preferences = prefs
            db.add(user)
            await db.commit()
            await db.refresh(user)

        return {"analysis": analysis, "preferences": user.preferences}


self_improvement_service = SelfImprovementService()
