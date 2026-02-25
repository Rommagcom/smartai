from __future__ import annotations

import base64
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from app.core.config import settings
from app.services.egress_policy_service import egress_policy_service
from app.services.http_client_service import http_client_service


class WebToolsService:
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
        if settings.SEARXNG_BASE_URL:
            return await self._searxng_search(query, limit)
        return await self._duckduckgo_search(query, limit)

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
        response = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
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
            page = await browser.new_page()
            await page.goto(safe_url, wait_until="domcontentloaded", timeout=timeout_ms)

            title = await page.title()
            current_url = page.url

            if action == "screenshot":
                image_bytes = await page.screenshot(full_page=True, type="png")
                await browser.close()
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
                await browser.close()
                return {
                    "action": action,
                    "url": current_url,
                    "title": title,
                    "mime_type": "application/pdf",
                    "file_name": "page.pdf",
                    "file_base64": base64.b64encode(pdf_bytes).decode("utf-8"),
                }

            text = await page.evaluate("document.body ? document.body.innerText : ''")
            await browser.close()
            normalized = "\n".join(line for line in text.splitlines() if line.strip())
            clipped = normalized[:max_chars]
            return {
                "action": "extract_text",
                "url": current_url,
                "title": title,
                "text": clipped,
                "truncated": len(normalized) > max_chars,
            }


web_tools_service = WebToolsService()
