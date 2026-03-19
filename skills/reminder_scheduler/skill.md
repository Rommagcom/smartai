# Reminder Scheduler Skill

This skill allows the model to create, list, and delete reminders or notifications with schedules.

## Tool
- Name: reminder_scheduler
- Actions: create, list, delete

## Schedule types
- once: one-time run at ISO datetime in `once_at`
- interval: repeated run each `interval_seconds`
- daily: repeated run at `time_of_day` in chosen `timezone`

## Trigger behavior
When a reminder is due, the bot scheduler sends `prompt` to the LLM as a user message and sends the resulting answer to the target Telegram chat.

## Tips
- Use `notify_text` to prepend a short message before the generated answer.
- Prefer `timezone` like `Europe/Moscow` for local-time schedules.
