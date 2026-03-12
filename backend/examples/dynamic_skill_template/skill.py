def run(params, context):
    city = params.get("city") or "Almaty"
    llm = (context or {}).get("llm", {})
    chat = llm.get("chat")

    summary = f"Local forecast for {city}: clear sky, +22C"
    if callable(chat):
        summary = chat(
            f"Rewrite this weather summary into one short friendly sentence: {summary}"
        )

    return {
        "ok": True,
        "city": city,
        "forecast": summary,
    }
