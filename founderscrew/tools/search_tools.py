import os
import httpx
import logging
from typing import List, Dict, Any
from founderscrew.config import settings

logger = logging.getLogger("founderscrew.search_tools")

def google_search(query: str) -> str:
    """Performs a Google search for grounding agent context.
    
    Uses Google Custom Search JSON API if GOOGLE_API_KEY and GOOGLE_CSE_ID are provided,
    otherwise falls back to a mock grounded search helper.
    """
    api_key = settings.get("google.api_key") or os.getenv("GOOGLE_API_KEY")
    cse_id = os.getenv("GOOGLE_CSE_ID")
    
    if not api_key or not cse_id:
        logger.warning("GOOGLE_API_KEY or GOOGLE_CSE_ID not configured. Using local grounding search mock.")
        return _get_mock_search_results(query)
        
    try:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": api_key,
            "cx": cse_id,
            "q": query
        }
        resp = httpx.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        
        items = data.get("items", [])
        if not items:
            return "No Google Search results found."
            
        results = []
        for item in items[:5]:  # Return top 5 results
            results.append(
                f"Title: {item.get('title')}\n"
                f"Link: {item.get('link')}\n"
                f"Snippet: {item.get('snippet')}\n"
            )
        return "\n---\n".join(results)
    except Exception as e:
        logger.error(f"Error calling Google Custom Search API: {e}. Falling back to mock search.")
        return _get_mock_search_results(query)


def _get_mock_search_results(query: str) -> str:
    """Mock search database providing common devops/git/python answers for local testing."""
    q = query.lower()
    
    if "pydantic" in q:
        return (
            "Title: Pydantic v2 Migration Guide\n"
            "Link: https://docs.pydantic.dev/2.0/migration/\n"
            "Snippet: Pydantic v2 includes major API changes: model_dump() replaces dict(), model_validate() replaces from_orm(), and model_validate_json() replaces parse_raw().\n"
        )
    elif "github" in q or "webhook" in q:
        return (
            "Title: GitHub Webhook Events and Payloads\n"
            "Link: https://docs.github.com/en/webhooks/webhook-events-and-payloads\n"
            "Snippet: Webhooks allow you to build or set up integrations which subscribe to certain events on GitHub.com. When one of those events is triggered, we'll send a HTTP POST payload to the webhook's configured URL.\n"
        )
    elif "fastapi" in q or "sse" in q:
        return (
            "Title: Server-Sent Events with FastAPI\n"
            "Link: https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse\n"
            "Snippet: To implement Server-Sent Events (SSE) in FastAPI, return a StreamingResponse that yields formatted strings: 'data: {message}\\n\\n'.\n"
        )
    else:
        return (
            f"Title: Search results for '{query}'\n"
            f"Link: https://www.google.com/search?q={query}\n"
            f"Snippet: Grounding context for search query '{query}'. (Mock search result for local/headless execution).\n"
        )
