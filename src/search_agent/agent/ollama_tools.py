from __future__ import annotations

import logging
import os

import ollama

logger = logging.getLogger(__name__)

ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

def web_search(query: str) -> str:
    """Search the web using ollama.web_search with OLLAMA_API_KEY."""
    try:
        api_key = os.getenv("OLLAMA_API_KEY", "").strip()
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        
        client = ollama.Client(host=ollama_url, headers=headers)

        
        result = client.web_search(query,max_results=5)
        return result if isinstance(result, str) else str(result)
    except Exception as e:
        logger.warning("Web search error for '%s': %s", query, e)
        return f"[Error] Web search unavailable: {e}"


def web_fetch(url: str) -> str:
    """Fetch webpage content using ollama.web_fetch with OLLAMA_API_KEY."""
    try:
        api_key = os.getenv("OLLAMA_API_KEY", "").strip()
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        client = ollama.Client(host=ollama_url, headers=headers)
        
        result = client.web_fetch(url)
        return result if isinstance(result, str) else str(result)
    except Exception as e:
        logger.warning("Web fetch error for '%s': %s", url, e)
        return f"[Error] Could not fetch {url}: {e}"
