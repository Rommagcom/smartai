from __future__ import annotations

import json
from typing import Any

from search_agent.reminders.store import ReminderOwnerContext, ReminderStore
from search_agent.security.rbac import RbacStore


def _text_value(value: Any) -> str:
    return str(value or "").strip()


def _resolve_prompt(args: dict[str, Any]) -> str:
    candidates = (
        args.get("prompt"),
        args.get("text"),
        args.get("message"),
        args.get("query"),
        args.get("task"),
        args.get("instruction"),
        args.get("input"),
        args.get("title"),
        args.get("notify_text"),
    )
    for candidate in candidates:
        value = _text_value(candidate)
        if value:
            return value
    return ""


def _flatten_nested_arguments(args: dict[str, Any]) -> dict[str, Any]:
    nested = args.get("arguments")
    if not isinstance(nested, dict):
        return args

    # Support tool runtimes that wrap payload as {"arguments": {...}}.
    merged = dict(nested)
    for key, value in args.items():
        if key == "arguments":
            continue
        merged[key] = value
    return merged


def _resolve_schedule_type(args: dict[str, Any]) -> str:
    explicit = _text_value(args.get("schedule_type")).lower()
    if explicit:
        return explicit

    if _text_value(args.get("cron_expr")):
        return "cron"
    if args.get("interval_seconds") is not None:
        return "interval"
    if _text_value(args.get("time_of_day")):
        return "daily"
    return "once"


def _audit_event(args: dict[str, Any], *, action: str, details: dict[str, object]) -> None:
    org_id = _text_value(args.get("org_id")) or "default-org"
    user_id = int(args["user_id"]) if args.get("user_id") is not None else 0
    try:
        rbac = RbacStore()
        rbac.audit(
            org_id=org_id,
            team_id="",
            actor_user_id=user_id,
            action=f"reminder.{action}",
            target_type="reminder",
            target_id=str(details.get("reminder_id") or ""),
            details=details,
        )
    except Exception:
        # Audit failures should not break business flow.
        return


def _handle_create(store: ReminderStore, args: dict[str, Any]) -> str:
    chat_id = args.get("chat_id")
    if chat_id is None:
        raise ValueError("chat_id is required for create action")

    resolved_prompt = _resolve_prompt(args)
    if not resolved_prompt:
        raise ValueError(
            "prompt is required for create action (accepted aliases: prompt, text, message, query, task)"
        )

    record = store.create_reminder(
        owner=ReminderOwnerContext(
            org_id=_text_value(args.get("org_id")) or "default-org",
            user_id=int(args["user_id"]) if args.get("user_id") is not None else 0,
        ),
        chat_id=int(chat_id),
        prompt=resolved_prompt,
        title=_text_value(args.get("title")) or "Reminder",
        notify_text=_text_value(args.get("notify_text")),
        schedule_type=_resolve_schedule_type(args),
        once_at=_text_value(args.get("once_at")) or None,
        interval_seconds=int(args["interval_seconds"]) if args.get("interval_seconds") is not None else None,
        time_of_day=_text_value(args.get("time_of_day")) or None,
        cron_expr=_text_value(args.get("cron_expr")) or None,
        timezone=_text_value(args.get("timezone")) or "UTC",
        max_runs=int(args["max_runs"]) if args.get("max_runs") is not None else None,
    )
    _audit_event(
        args,
        action="create",
        details={
            "reminder_id": record.id,
            "schedule_type": record.schedule_type,
            "chat_id": record.chat_id,
        },
    )
    return json.dumps(
        {
            "status": "created",
            "reminder": record.to_dict(),
        },
        ensure_ascii=True,
    )


def _handle_list(store: ReminderStore, args: dict[str, Any]) -> str:
    reminders = store.list_reminders(
        org_id=_text_value(args.get("org_id")) or None,
        user_id=int(args["user_id"]) if args.get("user_id") is not None else None,
        chat_id=int(args["chat_id"]) if args.get("chat_id") is not None else None,
        active_only=bool(args.get("active_only", True)),
    )
    _audit_event(
        args,
        action="list",
        details={
            "count": len(reminders),
            "chat_id": int(args["chat_id"]) if args.get("chat_id") is not None else None,
        },
    )
    return json.dumps(
        {
            "status": "ok",
            "count": len(reminders),
            "reminders": [item.to_dict() for item in reminders],
        },
        ensure_ascii=True,
    )


def _handle_delete(store: ReminderStore, args: dict[str, Any]) -> str:
    reminder_id = _text_value(args.get("reminder_id"))
    if not reminder_id:
        raise ValueError("reminder_id is required for delete action")

    removed = store.delete_reminder(
        reminder_id,
        org_id=_text_value(args.get("org_id")) or None,
        user_id=int(args["user_id"]) if args.get("user_id") is not None else None,
        chat_id=int(args["chat_id"]) if args.get("chat_id") is not None else None,
    )
    _audit_event(
        args,
        action="delete",
        details={
            "reminder_id": reminder_id,
            "removed": removed,
            "chat_id": int(args["chat_id"]) if args.get("chat_id") is not None else None,
        },
    )
    return json.dumps(
        {
            "status": "deleted" if removed else "not_found",
            "reminder_id": reminder_id,
        },
        ensure_ascii=True,
    )


def reminder_scheduler(action: str, **kwargs: Any) -> str:
    kwargs = _flatten_nested_arguments(kwargs)
    store = ReminderStore()
    normalized = _text_value(action).lower()

    if normalized == "create":
        return _handle_create(store, kwargs)
    if normalized == "list":
        return _handle_list(store, kwargs)
    if normalized == "delete":
        return _handle_delete(store, kwargs)

    raise ValueError("action must be one of: create, list, delete")
