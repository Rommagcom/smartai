"""Web search service — DuckDuckGo via ddgs."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ddgs import DDGS

logger = logging.getLogger(__name__)

# Maximum results per search
_DEFAULT_MAX_RESULTS = 5


class WebSearchService:
    """Thin wrapper around DDGS for web searches."""

    async def search(
        self,
        query: str,
        *,
        max_results: int = _DEFAULT_MAX_RESULTS,
        region: str = "wt-wt",
    ) -> dict[str, Any]:
        """Run a DuckDuckGo text search and return structured results.

        Returns dict compatible with tool_orchestrator result format:
        {
            "query": str,
            "results": [{"title", "snippet", "url"}, ...],
            "results_count": int,
        }
        """
        if not query.strip():
            return {"query": query, "results": [], "results_count": 0}

        safe_max = max(1, min(max_results, 10))

        try:
            raw = await asyncio.to_thread(
                self._search_sync, query, safe_max, region,
            )
        except Exception:
            logger.warning("DuckDuckGo search failed for query=%s", query[:120], exc_info=True)
            return {
                "query": query,
                "results": [],
                "results_count": 0,
                "error": "Search request failed",
            }

        results: list[dict[str, str]] = [
            {
                "title": str(r.get("title") or ""),
                "snippet": str(r.get("body") or ""),
                "url": str(r.get("href") or ""),
            }
            for r in raw
        ]

        return {
            "query": query,
            "results": results,
            "results_count": len(results),
        }

    @staticmethod
    def _search_sync(query: str, max_results: int, region: str) -> list[dict]:
        ddgs = DDGS(timeout=10)
        return ddgs.text(query, max_results=max_results, region=region)


web_search_service = WebSearchService()
