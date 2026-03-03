from __future__ import annotations

import base64
import logging
import re
from urllib.parse import quote_plus
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from app.core.config import settings
from app.services.egress_policy_service import egress_policy_service
from app.services.http_client_service import http_client_service

logger = logging.getLogger(__name__)

_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class WebToolsService:
    @staticmethod
    def _is_weather_query(query: str) -> bool:
        q = str(query or "").lower()
        tokens = (
            "погод",
            "weather",
            "температур",
            "осадки",
            "ветер",
            "прогноз",
        )
        return any(token in q for token in tokens)

    @staticmethod
    def _weather_fallback_results(query: str, limit: int) -> list[dict]:
        encoded_query = quote_plus(query)
        candidates = [
            {
                "title": "Yandex Погода",
                "url": "https://yandex.kz/pogoda/almaty",
                "snippet": "Почасовой и недельный прогноз.",
                "engine": "fallback-weather",
            },
            {
                "title": "Sinoptik Алматы",
                "url": "https://sinoptik.ua/погода-алматы",
                "snippet": "Подробный прогноз погоды и осадков.",
                "engine": "fallback-weather",
            },
            {
                "title": "Gismeteo Алматы",
                "url": "https://www.gismeteo.kz/weather-almaty-5205/",
                "snippet": "Температура, ветер, осадки.",
                "engine": "fallback-weather",
            },
            {
                "title": "DuckDuckGo weather query",
                "url": f"https://duckduckgo.com/?q={encoded_query}",
                "snippet": "Результаты поиска погоды по запросу.",
                "engine": "fallback-weather",
            },
        ]
        return candidates[: max(1, min(limit, len(candidates)))]

    @staticmethod
    def _is_currency_query(query: str) -> bool:
        q = str(query or "").lower()
        tokens = (
            "курс", "валют", "usd", "eur", "rub", "kzt",
            "тенге", "доллар", "евро", "рубл", "currency", "exchange rate",
        )
        return any(t in q for t in tokens)

    @staticmethod
    def _currency_fallback_results(query: str, limit: int) -> list[dict]:
        encoded_query = quote_plus(query)
        candidates = [
            {
                "title": "Нацбанк РК — Курсы валют",
                "url": "https://nationalbank.kz/ru/exchangerates/ezhednevnye-oficialnye-rynochnye-kursy-valyut",
                "snippet": "Ежедневные официальные рыночные курсы валют Национального Банка РК.",
                "engine": "fallback-currency",
            },
            {
                "title": "Google Finance",
                "url": f"https://www.google.com/finance/quote/USD-KZT?q={encoded_query}",
                "snippet": "Курсы валют в реальном времени.",
                "engine": "fallback-currency",
            },
            {
                "title": "Myfin.kz курсы",
                "url": "https://myfin.kz/currency/almaty",
                "snippet": "Курсы валют в обменных пунктах Алматы.",
                "engine": "fallback-currency",
            },
        ]
        return candidates[: max(1, min(limit, len(candidates)))]

    @staticmethod
    def _generic_fallback_results(query: str, limit: int) -> list[dict]:
        encoded_query = quote_plus(query)
        candidates = [
            {
                "title": "DuckDuckGo search",
                "url": f"https://duckduckgo.com/?q={encoded_query}",
                "snippet": "Откройте результаты DuckDuckGo по запросу.",
                "engine": "fallback-generic",
            },
            {
                "title": "Google search",
                "url": f"https://www.google.com/search?q={encoded_query}",
                "snippet": "Откройте результаты Google по запросу.",
                "engine": "fallback-generic",
            },
            {
                "title": "Yandex search",
                "url": f"https://yandex.kz/search/?text={encoded_query}",
                "snippet": "Откройте результаты Yandex по запросу.",
                "engine": "fallback-generic",
            },
        ]
        return candidates[: max(1, min(limit, len(candidates)))]

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only http/https URLs are allowed")
        if not parsed.netloc:
            raise ValueError("Invalid URL")
        return egress_policy_service.validate_url(url)

    async def web_fetch(self, url: str, max_chars: int = 12000) -> dict:
        safe_url = self._validate_url(url)
        client = http_client_service.get()
        response = await client.get(safe_url, timeout=settings.WEB_FETCH_TIMEOUT_SECONDS, follow_redirects=True)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        text = response.text
        title = ""
        if "html" in content_type.lower() or "<html" in text.lower():
            soup = BeautifulSoup(text, "html.parser")
            if soup.title and soup.title.string:
                title = soup.title.string.strip()
            for script in soup(["script", "style", "noscript"]):
                script.extract()
            text = soup.get_text("\n", strip=True)

        normalized = "\n".join(line for line in text.splitlines() if line.strip())
        clipped = normalized[:max_chars]
        return {
            "url": str(response.url),
            "status_code": response.status_code,
            "title": title,
            "content_type": content_type,
            "text": clipped,
            "truncated": len(normalized) > max_chars,
        }

    async def web_search(self, query: str, limit: int = 5) -> dict:
        provider_error = ""
        try:
            if settings.SEARXNG_BASE_URL:
                result = await self._searxng_search(query, limit)
            else:
                result = await self._duckduckgo_search(query, limit)
        except Exception as exc:
            provider_error = str(exc)
            result = {
                "query": query,
                "results": [],
                "provider": "search-provider-error",
                "error": provider_error,
            }

        results = result.get("results") if isinstance(result, dict) else None
        if isinstance(results, list) and results:
            return result
        if self._is_weather_query(query):
            return {
                "query": query,
                "results": self._weather_fallback_results(query, limit),
                "provider": f"{result.get('provider', 'unknown')}+fallback-weather",
                "error": provider_error or result.get("error", ""),
            }
        if self._is_currency_query(query):
            return {
                "query": query,
                "results": self._currency_fallback_results(query, limit),
                "provider": f"{result.get('provider', 'unknown')}+fallback-currency",
                "error": provider_error or result.get("error", ""),
            }
        return {
            "query": query,
            "results": self._generic_fallback_results(query, limit),
            "provider": f"{result.get('provider', 'unknown')}+fallback-generic",
            "error": provider_error or result.get("error", ""),
        }

    async def _searxng_search(self, query: str, limit: int) -> dict:
        client = http_client_service.get()
        response = await client.get(
            settings.SEARXNG_BASE_URL.rstrip("/") + "/search",
            params={"q": query, "format": "json"},
            timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()

        results: list[dict] = []
        for item in payload.get("results", [])[:limit]:
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", ""),
                    "engine": item.get("engine", "searxng"),
                }
            )
        return {"query": query, "results": results, "provider": "searxng"}

    async def _duckduckgo_search(self, query: str, limit: int) -> dict:
        client = http_client_service.get()

        # Try HTML endpoint first
        result = await self._duckduckgo_html(client, query, limit)
        if result.get("results"):
            return result

        # Fallback to Lite endpoint
        logger.info("DuckDuckGo HTML returned 0 results, trying Lite for: %s", query)
        return await self._duckduckgo_lite(client, query, limit)

    async def _duckduckgo_html(self, client: httpx.AsyncClient, query: str, limit: int) -> dict:
        response = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": _CHROME_UA},
            timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        results: list[dict] = []
        for result in soup.select("div.result"):
            title_el = result.select_one("a.result__a")
            snippet_el = result.select_one("a.result__snippet") or result.select_one("div.result__snippet")
            if not title_el:
                continue
            raw_href = title_el.get("href", "")
            parsed_href = self._decode_duckduckgo_redirect(raw_href)
            results.append(
                {
                    "title": title_el.get_text(" ", strip=True),
                    "url": parsed_href,
                    "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else "",
                    "engine": "duckduckgo-html",
                }
            )
            if len(results) >= limit:
                break

        return {"query": query, "results": results, "provider": "duckduckgo-html"}

    async def _duckduckgo_lite(self, client: httpx.AsyncClient, query: str, limit: int) -> dict:
        response = await client.get(
            "https://lite.duckduckgo.com/lite/",
            params={"q": query},
            headers={"User-Agent": _CHROME_UA},
            timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        results: list[dict] = []
        for link in soup.select("a.result-link") or soup.select("td a[href^='http']"):
            href = str(link.get("href", "")).strip()
            if not href or "duckduckgo.com" in href:
                continue
            results.append(
                {
                    "title": link.get_text(" ", strip=True),
                    "url": unquote(href),
                    "snippet": "",
                    "engine": "duckduckgo-lite",
                }
            )
            if len(results) >= limit:
                break

        return {"query": query, "results": results, "provider": "duckduckgo-lite"}

    @staticmethod
    def _decode_duckduckgo_redirect(url: str) -> str:
        if "/l/?" not in url:
            return url
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        uddg = params.get("uddg", [""])[0]
        return unquote(uddg) if uddg else url

    async def browser_action(
        self,
        url: str,
        action: str = "extract_text",
        max_chars: int = 8000,
        timeout_seconds: int = 30,
    ) -> dict:
        safe_url = self._validate_url(url)
        timeout_ms = timeout_seconds * 1000
        launch_kwargs: dict = {"headless": settings.BROWSER_HEADLESS}
        if settings.CHROME_EXECUTABLE_PATH:
            launch_kwargs["executable_path"] = settings.CHROME_EXECUTABLE_PATH

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs)
            try:
                page = await browser.new_page()
                try:
                    await page.goto(safe_url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception:
                    logger.warning("domcontentloaded timed out for %s, retrying with commit", safe_url)
                    await page.goto(safe_url, wait_until="commit", timeout=timeout_ms)

                title = await page.title()
                current_url = page.url

                if action == "screenshot":
                    image_bytes = await page.screenshot(full_page=True, type="png")
                    return {
                        "action": action,
                        "url": current_url,
                        "title": title,
                        "mime_type": "image/png",
                        "file_name": "screenshot.png",
                        "file_base64": base64.b64encode(image_bytes).decode("utf-8"),
                    }

                if action == "pdf":
                    pdf_bytes = await page.pdf(format="A4", print_background=True)
                    return {
                        "action": action,
                        "url": current_url,
                        "title": title,
                        "mime_type": "application/pdf",
                        "file_name": "page.pdf",
                        "file_base64": base64.b64encode(pdf_bytes).decode("utf-8"),
                    }

                text = await page.evaluate("document.body ? document.body.innerText : ''")
                normalized = "\n".join(line for line in text.splitlines() if line.strip())
                clipped = normalized[:max_chars]
                return {
                    "action": "extract_text",
                    "url": current_url,
                    "title": title,
                    "text": clipped,
                    "truncated": len(normalized) > max_chars,
                }
            finally:
                await browser.close()


web_tools_service = WebToolsService()
