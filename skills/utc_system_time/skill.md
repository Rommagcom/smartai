# UTC System Time

This default system skill returns the current date and time in UTC.

## Tool
- Name: `utc_system_time`
- Behavior: returns UTC datetime metadata (`iso`, `date`, `time`, `unix`).

## Usage
Call without arguments:

```json
{}
```

Example response:

```json
{"timezone":"UTC","iso":"2026-04-10T12:34:56+00:00","date":"2026-04-10","time":"12:34:56","unix":1775824496}
```
