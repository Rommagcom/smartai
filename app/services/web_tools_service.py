from __future__ import annotations

import base64
import logging
from urllib.parse import quote_plus
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from app.core.config import settings
from app.services.egress_policy_service import egress_policy_service
from app.services.http_client_service import http_client_service

logger = logging.getLogger(__name__)


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
    def _is_currency_query(query: str) -> bool:
        q = str(query or "").lower()
        tokens = (
            "курс",
            "валют",
            "доллар",
            "евро",
            "тенге",
            "рубл",
            "bitcoin",
            "биткоин",
            "обмен",
            "exchange rate",
            "usd",
            "eur",
            "kzt",
            "rub",
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
    def _currency_fallback_results(query: str, limit: int) -> list[dict]:
        encoded_query = quote_plus(query)
        candidates = [
            {
                "title": "Google Finance",
                "url": f"https://www.google.com/finance/quote/USD-KZT",
                "snippet": "Курс USD/KZT в реальном времени.",
                "engine": "fallback-currency",
            },
            {
                "title": "Нацбанк РК — курсы валют",
                "url": "https://nationalbank.kz/ru/exchangerates/ezhednevnye-oficialnye-rynochnye-kursy-valyut",
                "snippet": "Ежедневные официальные (рыночные) курсы валют.",
                "engine": "fallback-currency",
            },
            {
                "title": "Myfin.kz — курсы валют",
                "url": "https://myfin.kz/currency/almaty",
                "snippet": "Курсы обмена валют в банках Алматы.",
                "engine": "fallback-currency",
            },
            {
                "title": "DuckDuckGo currency query",
                "url": f"https://duckduckgo.com/?q={encoded_query}",
                "snippet": "Результаты поиска по валютному запросу.",
                "engine": "fallback-currency",
            },
        ]
        return candidates[: max(1, min(limit, len(candidates)))]

    @staticmethod
    def _general_fallback_results(query: str, limit: int) -> list[dict]:
        """When DuckDuckGo returns nothing, provide a DuckDuckGo link so web_fetch
        or the user can still follow through."""
        encoded_query = quote_plus(query)
        return [
            {
                "title": f"Поиск: {query}",
                "url": f"https://duckduckgo.com/?q={encoded_query}",
                "snippet": "Прямая ссылка на поисковую выдачу.",
                "engine": "fallback-general",
            },
        ][:limit]

    @staticmethod
    async def _validate_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only http/https URLs are allowed")
        if not parsed.netloc:
            raise ValueError("Invalid URL")
        return await egress_policy_service.validate_url(url)

    async def web_fetch(self, url: str, max_chars: int = 12000) -> dict:
        safe_url = await self._validate_url(url)
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
        try:
            if settings.SEARXNG_BASE_URL:
                result = await self._searxng_search(query, limit)
            else:
                result = await self._duckduckgo_search(query, limit)
        except Exception as exc:
            logger.warning("Primary web_search failed (%s), using fallback", exc)
            result = {"query": query, "results": [], "provider": "error"}

        results = result.get("results") if isinstance(result, dict) else None
        if isinstance(results, list) and results:
            return result

        # Primary search returned no results — try topic-specific fallbacks.
        provider = result.get("provider", "unknown") if isinstance(result, dict) else "unknown"
        logger.info("web_search empty from '%s', trying fallback for query: %s", provider, query[:120])

        if self._is_weather_query(query):
            return {
                "query": query,
                "results": self._weather_fallback_results(query, limit),
                "provider": f"{provider}+fallback-weather",
            }
        if self._is_currency_query(query):
            return {
                "query": query,
                "results": self._currency_fallback_results(query, limit),
                "provider": f"{provider}+fallback-currency",
            }

        # Generic fallback — at least give something for web_fetch to follow.
        return {
            "query": query,
            "results": self._general_fallback_results(query, limit),
            "provider": f"{provider}+fallback-general",
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
        ddg_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.5",
        }

        # Try the primary HTML endpoint first.
        results = await self._parse_duckduckgo_html(
            client, "https://html.duckduckgo.com/html/", query, limit, ddg_headers,
        )
        if results:
            return {"query": query, "results": results, "provider": "duckduckgo-html"}

        # Fallback: DuckDuckGo Lite — different HTML structure, less likely to block.
        logger.info("DuckDuckGo HTML returned 0 results, retrying with Lite for: %s", query[:100])
        results = await self._parse_duckduckgo_lite(
            client, query, limit, ddg_headers,
        )
        return {"query": query, "results": results, "provider": "duckduckgo-lite"}

    async def _parse_duckduckgo_html(
        self,
        client: httpx.AsyncClient,
        url: str,
        query: str,
        limit: int,
        headers: dict,
    ) -> list[dict]:
        response = await client.get(
            url,
            params={"q": query},
            headers=headers,
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
        return results

    async def _parse_duckduckgo_lite(
        self,
        client: httpx.AsyncClient,
        query: str,
        limit: int,
        headers: dict,
    ) -> list[dict]:
        """Parse DuckDuckGo Lite — simpler page, different HTML selectors."""
        try:
            response = await client.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query},
                headers=headers,
                timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning("DuckDuckGo Lite also failed: %s", exc)
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        results: list[dict] = []
        # Lite uses <a class="result-link"> or simple <a> inside result table rows.
        for link in soup.select("a.result-link") or soup.select("td a[href^='http']"):
            href = str(link.get("href", "")).strip()
            if not href or "duckduckgo.com" in href:
                continue
            title_text = link.get_text(" ", strip=True)
            # Try to pick up the next sibling text as snippet.
            snippet = ""
            snippet_td = link.find_parent("tr")
            if snippet_td:
                next_row = snippet_td.find_next_sibling("tr")
                if next_row:
                    snippet = next_row.get_text(" ", strip=True)[:300]
            results.append(
                {
                    "title": title_text,
                    "url": self._decode_duckduckgo_redirect(href),
                    "snippet": snippet,
                    "engine": "duckduckgo-lite",
                }
            )
            if len(results) >= limit:
                break
        return results

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
        safe_url = await self._validate_url(url)
        timeout_ms = timeout_seconds * 1000
        launch_kwargs: dict = {"headless": settings.BROWSER_HEADLESS}
        if settings.CHROME_EXECUTABLE_PATH:
            launch_kwargs["executable_path"] = settings.CHROME_EXECUTABLE_PATH

        browser = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(**launch_kwargs)
                page = await browser.new_page()
                try:
                    await page.goto(safe_url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception:
                    # Fallback: some pages never fire 'domcontentloaded'.
                    # Try with 'commit' (first bytes received) instead of giving up.
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
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass


web_tools_service = WebToolsService()
