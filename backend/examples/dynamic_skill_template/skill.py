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
        llm_response = chat(
            system="You are a concise weather assistant. Reply in one short friendly sentence.",
            user=(
                f"City: {city}. Draft summary: {summary}. "
                f"API data: {weather.get('body')}"
            ),
            options={"max_tokens": 80},
        )
        if isinstance(llm_response, dict):
            summary = str(llm_response.get("text") or summary)
        elif isinstance(llm_response, str) and llm_response.strip():
            summary = llm_response.strip()

    return {
        "ok": True,
        "city": city,
        "weather": weather.get("body"),
        "forecast": summary,
    }
