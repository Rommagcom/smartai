def run(params, context):
    city = params.get("city") or "Almaty"
    llm = (context or {}).get("llm", {})
    http = (context or {}).get("http", {})
    chat = llm.get("chat")
    http_get = http.get("get")

    summary = f"Local forecast for {city}: clear sky, +22C"
    weather = {}
    if callable(http_get):
        weather = http_get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": 43.2389,
                "longitude": 76.8897,
                "current": "temperature_2m",
            },
        )
    if callable(chat):
        summary = chat(
            f"Rewrite this weather summary into one short friendly sentence: {summary}. API data: {weather.get('body')}"
        )

    return {
        "ok": True,
        "city": city,
        "weather": weather.get("body"),
        "forecast": summary,
    }
