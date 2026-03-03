from datetime import datetime, timezone

from app.models.user import User

SOUL_FIRST_QUESTION = "Расскажи кто вы и чем занимаетесь?"

DEFAULT_SOUL_TEMPLATE = """Ты — персональный AI-ассистент. Не чат-бот, а инструмент для решения задач.

## CORE PRINCIPLES

1. **Будь полезным, не показным**
   - Не пиши \"Отличный вопрос!\" и \"Я с удовольствием помогу!\"
   - Действуй. Отвечай по существу.

2. **Имеешь право на мнение**
   - Дискомфорт? Говори.
   - Что-то глупое? Скажи прямо.
   - Предпочитаешь вариант А над Б? Объясни почему.

3. **Ресурсный до упора**
   - Сначала попробуй сам: поищи, прочитай, выполни.
   - Потом спрашивай.

4. **Запоминай контекст**
- Используй историю диалога и факты пользователя из memory.
- При необходимости сохраняй важные факты через memory.
- Не выдумывай то, чего нет в контексте.

## BEHAVIOR

- **Краткость там, где важна скорость**
- **Детали там, где важна точность**
- **Никакого корпоративного буллшита**
- **Ты не голос пользователя** — в группах молчи, если не спросили по имени

## TOOLS

У тебя есть доступ к:
- execute_python (sandbox)
- memory_add, memory_list, memory_search, memory_delete, memory_delete_all
- doc_search (поиск по загруженным документам)
- cron_add, cron_list, cron_delete (задачи по расписанию)
- integration_add, integrations_list, integration_call

## SAFETY

- Не отправляй ерунду в продакшн
- Не раскрывай чужие данные
- Не используй матершинные слова и не цензурную речь
- Если инструмент недоступен или упал — честно сообщи и предложи следующий шаг
- Если данных недостаточно — задай уточняющий вопрос
"""

TONE_OPTIONS = {
    "my_style": "Прямой, без воды, немного саркастичный",
    "formal": "Деловой, структурированный, вежливый",
    "friendly": "Тёплый, поддерживающий, с эмодзи",
    "geek": "Технический, детальный, с жаргоном",
}

STYLE_OPTIONS = {
    "direct": "Прямой",
    "business": "Деловой",
    "sarcastic": "Саркастичный",
    "friendly": "Дружелюбный",
}

TASK_OPTIONS = ["business-analysis", "devops", "creativity", "coding", "other"]


class SoulService:
    def get_onboarding_payload(self, user: User) -> dict:
        if user.soul_configured:
            return {}
        return {
            "first_question": SOUL_FIRST_QUESTION,
            "tone_options": TONE_OPTIONS,
            "task_options": TASK_OPTIONS,
            "styles": STYLE_OPTIONS,
            "hint": "Заполните /users/me/soul/setup перед первым чатом",
        }

    def get_next_onboarding_step(self, user: User) -> dict:
        profile = dict(user.soul_profile or {})

        if user.soul_configured:
            return {
                "step": "done",
                "done": True,
                "required_fields": [],
                "next_action": "assistant_ready",
                "prompt": "Smart AI уже настроен. Можете писать в чат.",
                "hints": {
                    "adapt": "Для смены профиля задачи используйте /api/v1/users/me/soul/adapt-task",
                },
            }

        if not profile.get("assistant_name") or not profile.get("emoji"):
            return {
                "step": "identity",
                "done": False,
                "required_fields": ["assistant_name", "emoji"],
                "next_action": "collect_identity",
                "prompt": "Выберите имя и эмодзи ассистента.",
                "hints": {
                    "default_name": "SmartAi",
                    "default_emoji": "🧠",
                },
            }

        if not profile.get("style") or not profile.get("tone_modifier"):
            return {
                "step": "tone",
                "done": False,
                "required_fields": ["style", "tone_modifier"],
                "next_action": "collect_tone",
                "prompt": "Выберите стиль и тональность ответов.",
                "hints": TONE_OPTIONS,
            }

        if not profile.get("task_mode"):
            return {
                "step": "task_mode",
                "done": False,
                "required_fields": ["task_mode"],
                "next_action": "collect_task_mode",
                "prompt": "Под какой класс задач адаптировать ассистента?",
                "hints": {"options": ", ".join(TASK_OPTIONS)},
            }

        if not profile.get("user_description"):
            return {
                "step": "confirm",
                "done": False,
                "required_fields": ["user_description"],
                "next_action": "collect_user_description",
                "prompt": SOUL_FIRST_QUESTION,
                "hints": {
                    "example": "Я backend-разработчик, строим AI-ассистента для задач команды",
                },
            }

        return {
            "step": "confirm",
            "done": False,
            "required_fields": ["user_description"],
            "next_action": "submit_setup",
            "prompt": "Перед первым чатом отправьте /api/v1/users/me/soul/setup с выбранными параметрами.",
            "hints": {
                "setup_endpoint": "/api/v1/users/me/soul/setup",
            },
        }

    @staticmethod
    def build_system_prompt(
        assistant_name: str,
        emoji: str,
        style: str,
        tone_modifier: str,
        task_mode: str,
        user_description: str,
    ) -> str:
        tone_text = tone_modifier or TONE_OPTIONS.get("my_style")
        style_text = STYLE_OPTIONS.get(style, STYLE_OPTIONS["direct"])
        return (
            f"{DEFAULT_SOUL_TEMPLATE}\n"
            f"## IDENTITY\n"
            f"- Имя: {assistant_name}\n"
            f"- Эмодзи: {emoji}\n"
            f"- Стиль: {style_text}\n"
            f"- Тональность: {tone_text}\n\n"
            f"## ONBOARDING\n"
            f"- Первый вопрос пользователю: {SOUL_FIRST_QUESTION}\n"
            f"- Ответ пользователя: {user_description}\n\n"
            f"## TASK ADAPTATION\n"
            f"- Текущий профиль задач: {task_mode}\n"
            f"- Если профиль неясен, спроси: 'Хочешь, я адаптируюсь под конкретную задачу? (бизнес-анализ, DevOps, творчество, код или другое)'\n"
        )

    def get_status(self, user: User) -> dict:
        soul_profile = user.soul_profile or {}
        return {
            "configured": user.soul_configured,
            "first_question": SOUL_FIRST_QUESTION,
            "template_preview": DEFAULT_SOUL_TEMPLATE,
            "tone_options": TONE_OPTIONS,
            "task_options": TASK_OPTIONS,
            "updated_at": soul_profile.get("updated_at"),
        }

    def setup_user_soul(
        self,
        user: User,
        user_description: str,
        assistant_name: str | None,
        emoji: str | None,
        style: str,
        tone_modifier: str | None,
        task_mode: str,
    ) -> User:
        name = assistant_name or "Smart Ai"
        selected_emoji = emoji or "🧠"
        selected_style = style if style in STYLE_OPTIONS else "direct"
        selected_task_mode = task_mode if task_mode in TASK_OPTIONS else "other"
        selected_tone = tone_modifier or TONE_OPTIONS.get("my_style")

        user.system_prompt_template = self.build_system_prompt(
            assistant_name=name,
            emoji=selected_emoji,
            style=selected_style,
            tone_modifier=selected_tone,
            task_mode=selected_task_mode,
            user_description=user_description,
        )

        user.soul_configured = True
        user.soul_profile = {
            "assistant_name": name,
            "emoji": selected_emoji,
            "style": selected_style,
            "tone_modifier": selected_tone,
            "task_mode": selected_task_mode,
            "user_description": user_description,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        preferences = dict(user.preferences or {})
        preferences["style"] = selected_style
        preferences["task_mode"] = selected_task_mode
        user.preferences = preferences
        return user

    def adapt_task(self, user: User, task_mode: str, custom_task: str | None = None) -> User:
        profile = dict(user.soul_profile or {})
        selected_task_mode = task_mode if task_mode in TASK_OPTIONS else "other"
        if custom_task:
            selected_task_mode = f"other:{custom_task}"

        profile["task_mode"] = selected_task_mode
        profile["updated_at"] = datetime.now(timezone.utc).isoformat()
        user.soul_profile = profile

        user.system_prompt_template = self.build_system_prompt(
            assistant_name=profile.get("assistant_name", "Smart Ai"),
            emoji=profile.get("emoji", "🧠"),
            style=profile.get("style", "direct"),
            tone_modifier=profile.get("tone_modifier", TONE_OPTIONS.get("my_style")),
            task_mode=selected_task_mode,
            user_description=profile.get("user_description", ""),
        )

        preferences = dict(user.preferences or {})
        preferences["task_mode"] = selected_task_mode
        user.preferences = preferences
        return user


soul_service = SoulService()
