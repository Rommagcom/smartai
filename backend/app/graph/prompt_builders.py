"""Centralized prompt builders for graph nodes.

All prompt construction logic is consolidated here to keep node files clean
and focused on orchestration rather than prompt formatting.
"""
from __future__ import annotations


def build_router_prompt(
    *,
    user_message: str,
    feedback_plan: str,
    tool_catalog_signatures: str,
    dynamic_tools_block: str,
    integrations_block: str,
    retrieved_block: str,
) -> str:
    """Build the router/planner prompt for intent classification and tool selection.
    
    Args:
        user_message: The current user query.
        feedback_plan: Feedback/hints from previous reasoning iteration, if any.
        tool_catalog_signatures: Formatted list of available static tools.
        dynamic_tools_block: Formatted block of user's registered dynamic tools.
        integrations_block: Formatted block of user's registered integrations.
        retrieved_block: Formatted block of semantically relevant tools from Milvus.
    
    Returns:
        Complete system prompt for the router LLM.
    """
    return (
        "Ты — маршрутизатор задач AI-агента.\n"
        "Твоя цель — выбрать правильный инструмент для выполнения запроса.\n\n"
        f"Вопрос пользователя: \"{user_message}\"\n\n"
        "Доступные инструменты:\n"
        f"{tool_catalog_signatures}\n"
        f"{dynamic_tools_block}"
        f"{integrations_block}"
        f"{retrieved_block}"
        "(Также всегда доступен инструмент 'web_search' для поиска в интернете)\n\n"
        "ОБРАТИ ВНИМАНИЕ НА ЗАМЕЧАНИЯ ПРЕДЫДУЩЕГО ШАГА:\n"
        f"{feedback_plan if feedback_plan else 'Это первый проход, замечаний нет.'}\n\n"
        "ИНСТРУКЦИЯ:\n"
        "Если в замечаниях сказано искать в интернете — выбирай decision='web_search' "
        "и формируй оптимальный query в steps.\n"
        "Если нужно дернуть внутреннее API/инструменты — выбирай decision='tool'.\n"
        "Если нужен обычный ответ без инструментов — decision='chat'.\n\n"
        "Верни JSON с полями: decision, steps, response_hint, confidence.\n"
        "decision: 'tool' — нужен инструмент, 'chat' — обычный разговор, "
        "'memory' — операция с памятью, 'clarify' — нужно уточнение, "
        "'web_search' — поиск информации в интернете.\n"
        "Правила:\n"
        "1) Для напоминаний используй cron_add с schedule_text и task_text. "
        "Если задача требует вызова API/интеграции (курс валют, погода и т.д.) — добавь action_type='chat'. "
        "Если обычное текстовое напоминание — action_type не нужен.\n"
        "2) Для PDF — pdf_create.\n"
        "2a) Для Excel/таблицы — excel_create.\n"
        "3) Если просит подключить API — register_api_tool с user_message (полным сообщением пользователя).\n"
        "4) Для удаления всех напоминаний — cron_delete_all.\n"
        "5) Не выдумывай аргументы.\n"
        "6) steps — максимум 5 шагов.\n"
        "7) Для удаления факта: memory_search → memory_delete.\n"
        "8) Для ВЫЗОВА подключённой интеграции используй integration_call "
        "с service_name из списка интеграций пользователя. "
        "Если пользователь пишет 'вызови интеграцию X', 'данные из X', 'курс валют из nationalbank' — "
        "это integration_call с service_name=X.\n"
        "9) Для пользовательских динамических API используй dyn:<имя> с нужными аргументами.\n"
        "10) Если в 'семантически найденных инструментах' есть подходящий — предпочитай его.\n"
        "11) Для списка загруженных документов — doc_list.\n"
        "12) Для удаления одного документа — doc_delete с source_doc (имя файла).\n"
        "13) Для удаления всех документов — doc_delete_all.\n"
        "14) Для поиска информации в интернете используй decision='web_search' с query в steps. "
        "Если пользователь просит 'найди в интернете', 'загугли', 'поищи в сети' — это web_search. "
        "Для регулярного получения данных из интернета — cron_add с action_type='chat' и task_text='найди в интернете ...'. \n"
        "15) Если шаг зависит от результата предыдущего, используй плейсхолдеры: "
        "$prev.body — тело ответа предыдущего шага, $prev.items, $prev.content и т.д. "
        "Пример: [{\"tool\": \"integration_call\", \"arguments\": {\"service_name\": \"X\"}}, "
        "{\"tool\": \"pdf_create\", \"arguments\": {\"title\": \"Отчёт\", \"content\": \"$prev.body\"}}].\n"
        "15a) Для одиночного шага pdf_create/excel_create НЕ пиши полный документ в arguments.content. "
        "Передавай только короткий источник (до 300 символов, без markdown-блоков и длинных переносов). "
        "Если нужен полный документ, сначала получи/сформируй данные отдельным шагом, затем используй $prev.body.\n"
        "16) Если пользователь просит актуальные данные (погода, курс валют, новости и т.п.) И одновременно экспорт в PDF/Excel, "
        "сначала получи данные (decision='web_search' или integration_call), затем сформируй файл по результатам. "
        "Нельзя сразу делать pdf_create/excel_create только из исходного текста запроса.\n"
    )


def build_compose_prompt(
    *,
    user_message: str,
    context_text: str,
    iterations: int,
    max_iterations: int,
    has_integration: bool,
    all_failed: bool,
    has_web_context: bool,
) -> str:
    """Build the compose/reflexion prompt for evaluating answer completeness.
    
    This prompt guides the LLM to determine whether collected data is sufficient
    to answer the user's question, or whether additional information gathering is needed.
    
    Args:
        user_message: The original user query.
        context_text: Concatenated data from all channels (web, tools, memory, etc.).
        iterations: Current iteration number (starting from 1).
        max_iterations: Maximum allowed iterations before forcing completion.
        has_integration: Whether integration results are in the context.
        all_failed: Whether all tool executions failed.
        has_web_context: Whether web search/fetch results are in the context.
    
    Returns:
        Complete system prompt for the compose LLM.
    """
    concise_style_prompt = (
        "\n\nСТИЛЬ ОТВЕТА (ОБЯЗАТЕЛЬНО):\n"
        "- Если пользователь не просил подробности, отвечай кратко и понятно: 1-3 предложений.\n"
        "- Для простых вопросов давай 'Да/Нет' или короткий факт без лишних пояснений.\n"
        "- Не добавляй вводные фразы вроде 'Конечно, ...', 'Вот что я думаю', если они не нужны.\n"
        "- Если пользователь явно просит детально, дай развернутый структурированный ответ."
    )

    integration_summary_prompt = ""
    if has_integration and not all_failed:
        integration_summary_prompt = (
            "\n\nДОПОЛНИТЕЛЬНО ДЛЯ ОТВЕТОВ ИНТЕГРАЦИЙ:\n"
            "Ты получил ответ от внешнего API (интеграции). "
            "Проанализируй тело ответа и сформируй ЧЕЛОВЕКОЧИТАЕМЫЙ ответ. "
            "Если данные в XML/JSON - извлеки ключевые значения и представь "
            "в удобном виде (таблица, список, текст). "
            "НЕ выводи сырой XML/JSON. НЕ обрезай данные - покажи ВСЕ основные записи. "
            "Если пользователь просил конкретные данные - выдели их."
        )

    web_formatting_prompt = ""
    if has_web_context:
        web_formatting_prompt = (
            "\n\nДОПОЛНИТЕЛЬНО ДЛЯ WEB-ОТВЕТОВ:\n"
            "Не используй markdown-таблицы. Предпочитай обычный текст и короткие списки. "
            "Ответ должен быть завершенным, без оборванных строк и незакрытого markdown."
        )

    return (
        "Ты — финальный проверяющий AI-агента. Твоя задача — проанализировать "
        "вопрос пользователя и собранные данные.\n\n"
        f"Вопрос пользователя: \"{user_message}\"\n"
        f"Собранные данные:\n{context_text if context_text else '(данные отсутствуют)'}\n\n"
        f"Текущая итерация поиска: {iterations} из {max_iterations}.\n\n"
        "ИНСТРУКЦИЯ:\n"
        "1. Оцени, достаточно ли собранных данных для точного, полного и правдивого ответа.\n"
        "2. Если данных ДОСТАТОЧНО:\n"
        "   - Сформируй итоговый ответ.\n"
        "   - Установи is_complete: true.\n"
        "   - feedback_plan оставь пустым.\n"
        "3. Если данных НЕДОСТАТОЧНО:\n"
        "   - Не пиши финальный ответ пользователю.\n"
        "   - Напиши четкую инструкцию (feedback_plan), что нужно найти на следующем шаге.\n"
        "   - Установи is_complete: false.\n\n"
        f"{integration_summary_prompt}{web_formatting_prompt}{concise_style_prompt}\n\n"
        "Ответь СТРОГО валидным JSON:\n"
        '{"is_complete": true | false, "answer": "...", "feedback_plan": "..."}'
    )


def build_chat_system_prompt(
    system_prompt: str,
    stm: list[str],
    ltm: list[str],
    rag: list[str],
    summary: str | None,
) -> str:
    """Build enriched system prompt for direct chat (no tools).
    
    Combines base system prompt with all available context layers:
    short-term memory, long-term memory, RAG context, and session summary.
    
    Args:
        system_prompt: Base system prompt from config.
        stm: Short-term memory items from current session.
        ltm: Long-term memory items (facts about the user).
        rag: Relevant document passages from RAG.
        summary: Optional high-level session summary.
    
    Returns:
        System prompt enriched with all memory layers.
    """
    parts = [system_prompt]

    if summary:
        parts.append(f"\n\n{summary}")
    if ltm:
        parts.append("\n\nДолгосрочная память:\n" + "\n".join(f"- {item}" for item in ltm))
    if stm:
        parts.append("\n\nКонтекст текущей сессии:\n" + "\n".join(f"- {item}" for item in stm))
    if rag:
        parts.append("\n\nРелевантные документы:\n" + "\n".join(f"- {item}" for item in rag))

    return "\n".join(parts)


def build_recovered_answer_prompt(
    user_message: str,
    web_context: str,
) -> tuple[list[dict], str]:
    """Build messages for recovering truncated/incomplete markdown answers.
    
    Used when compose LLM returns incomplete markdown-formatted answer,
    and we need to reconstruct a complete response using web/tool context.
    
    Args:
        user_message: Original user query.
        web_context: Combined web fetch / search results context.
    
    Returns:
        Tuple of (messages list, system prompt text).
    """
    system_prompt = (
        "Сформируй полный ответ по данным ниже. "
        "Пиши обычным текстом или коротким маркированным списком. "
        "Не используй markdown-таблицы и не оставляй ответ незавершенным."
    )
    
    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": f"Вопрос: {user_message}\n\nДанные:\n{web_context[:12000]}",
        },
    ]
    
    return messages, system_prompt


def build_doc_ask_compose_prompt(
    user_message: str,
    doc_answer: str,
    doc_context: str,
) -> tuple[list[dict], str]:
    """Build messages for composing refined doc_ask answer via LLM.
    
    Takes rough doc_ask tool output and refines it with source citations,
    creating a polished final answer for document-based Q&A.
    
    Args:
        user_message: Original user question about documents.
        doc_answer: Rough answer from doc_ask tool.
        doc_context: Formatted document chunks and scores.
    
    Returns:
        Tuple of (messages list, system prompt text).
    """
    system_prompt = (
        "Ты формируешь финальный ответ пользователю на основе результатов поиска по его документам. "
        "Учитывай формулировку вопроса пользователя. "
        "Не выдумывай факты вне предоставленного контекста. "
        "Пиши подробно и структурировано. "
        "В конце добавь раздел 'Источники' с кратким перечислением документов, на которые опираешься."
    )
    
    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": (
                f"Вопрос пользователя:\n{user_message}\n\n"
                f"Черновой ответ инструмента doc_ask:\n{doc_answer or '(пусто)'}\n\n"
                f"Фрагменты источников:\n{doc_context or '(источники не переданы)'}"
            ),
        },
    ]
    
    return messages, system_prompt


def build_web_fallback_prompt(
    user_message: str,
    web_context: str,
) -> tuple[list[dict], str]:
    """Build messages for fallback web answer synthesis when compose fails.
    
    Creates a short, precise answer from web context when the main compose
    flow encounters issues or doesn't yield a complete answer.
    
    Args:
        user_message: Original user query.
        web_context: Combined web fetch / search results context.
    
    Returns:
        Tuple of (messages list, system prompt text).
    """
    system_prompt = (
        "Сформируй короткий и точный ответ пользователю только по данным ниже. "
        "Если данных недостаточно, честно скажи, чего не хватает. "
        "Не используй markdown-таблицы. "
        "Если пользователь не просил подробности, отвечай кратко и понятно: 1-3 предложений. "
        "Для простых вопросов давай 'Да/Нет' или короткий факт без лишних пояснений. "
        "Не добавляй вводные фразы вроде 'Конечно, ...', 'Вот что я думаю', если они не нужны. "
        "Если пользователь явно просит детально, дай более развернутый структурированный ответ."
    )
    
    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": f"Вопрос: {user_message}\n\nДанные:\n{web_context[:12000]}",
        },
    ]
    
    return messages, system_prompt
