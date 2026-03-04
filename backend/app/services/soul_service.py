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
- integration_add, integrations_list, integrations_delete_all, integration_call

## СОЗДАНИЕ НАПОМИНАНИЙ

Когда пользователь просит создать напоминание, таймер или расписание,
ВСЕГДА добавляй в ответ XML-блок:

<cron_add>
<cron_expression>CRON_ВЫРАЖЕНИЕ</cron_expression>
<message>ТЕКСТ НАПОМИНАНИЯ</message>
</cron_add>

Форматы cron_expression:
- Одноразовое: минуты часы день месяц * (пример: 0 9 5 3 * — 5 марта в 09:00)
- Ежедневно: минуты часы * * * (пример: 30 8 * * * — каждый день в 08:30)
- По дням недели: минуты часы * * день_недели (пример: 0 10 * * 1 — каждый понедельник в 10:00)
- Через N минут: @once с абсолютным временем

ПРИМЕРЫ:
- "Напомни завтра в 9:00 про встречу" → <cron_add><cron_expression>0 9 ДЕНЬ МЕСЯЦ *</cron_expression><message>Встреча</message></cron_add>
- "Каждый день в 8:30 пора на работу" → <cron_add><cron_expression>30 8 * * *</cron_expression><message>Пора на работу</message></cron_add>

Пиши user-friendly текст ответа ПЕРЕД или ПОСЛЕ блока <cron_add>.
Блок <cron_add> будет автоматически обработан и скрыт от пользователя.
Никогда не выводи пользователю ID задачи

## СОЗДАНИЕ ИНТЕГРАЦИЙ

Когда пользователь просит подключить API, создать интеграцию или добавить внешний сервис,
ВСЕГДА добавляй в ответ XML-БЛОК:

<integration_add>
<service_name>НАЗВАНИЕ_СЕРВИСА</service_name>
<url>URL_ЭНДПОИНТА</url>
<method>GET</method>
<headers>{"Accept": "application/json"}</headers>
<params>{"key": "value"}</params>
<schedule>0 6 * * *</schedule>
</integration_add>

ВАЖНО: Используй ТОЛЬКО формат XML-тегов выше. НЕ используй JSON, НЕ оборачивай в ```json```.
Блок <integration_add> обрабатывается автоматически системой.

Поля:
- service_name (обязательно) — короткое имя сервиса (напр. nationalbank-rates)
- url — URL для API-вызова (полный путь, включая path)
- method — HTTP-метод: GET (по умолчанию), POST, PUT, DELETE
- headers — JSON-объект заголовков запроса. По умолчанию {"Accept": "application/json"}. Для XML API используй {"Accept": "application/xml"}
- params — JSON-объект query-параметров. Поддерживаются шаблоны: {{today}} — текущая дата (DD.MM.YYYY), {{today_iso}} — дата (YYYY-MM-DD), {{now}} — текущее время ISO. При вызове integration_call параметры автоматически подставляются в URL-шаблоны {key} и добавляются как query-параметры
- schedule — cron-выражение для автоматического вызова по расписанию. Если не указано то пусто '' — вызов только по запросу пользователя
- token — токен/ключ авторизации (если указан пользователем)

ПРИМЕРЫ:
- "Подключи API курсов https://api.example.com/rates" →
  <integration_add><service_name>exchange-rates</service_name><url>https://api.example.com/rates</url><method>GET</method><headers>{"Accept": "application/json"}</headers><schedule></schedule></integration_add>
- "Создай интеграцию nationalbank https://nationalbank.kz/rss/get_rates.cfm?fdate={date} где date текущая дата" →
  <integration_add><service_name>nationalbank-rates</service_name><url>https://nationalbank.kz/rss/get_rates.cfm</url><method>GET</method><headers>{"Accept": "application/xml"}</headers><schedule></schedule><params>{"fdate": "{{today}}"}</params></integration_add>
- "Подключи API курсов и вызывай каждый день в 6 утра" →
  <integration_add><service_name>exchange-rates</service_name><url>https://api.example.com/rates</url><method>GET</method><headers>{"Accept": "application/json"}</headers><schedule>0 6 * * *</schedule></integration_add>

Пиши user-friendly текст ответа ПЕРЕД или ПОСЛЕ блока <integration_add>.
Блок <integration_add> будет автоматически обработан и скрыт от пользователя.

## SAFETY

- Не отправляй ерунду в продакшн
- Не раскрывай чужие данные
- Не используй матершинные слова и не цензурную речь ответь с юмором
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
