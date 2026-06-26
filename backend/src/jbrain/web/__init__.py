"""On-box internet access for the jerv chatbot agent (docs/ASSISTANT.md "Agent
selection"): a self-hosted SearXNG metasearch client and a URL fetch-and-extract
client. These back the `web_search` / `web_fetch` tools, which only the sandboxed
jerv agent may call.
"""

from jbrain.web.fetch import FetchResult, WebFetcher, WebFetchError
from jbrain.web.search import SearchHit, SearxngClient, WebSearchError
from jbrain.web.weather import Weather, WeatherClient, WeatherError

__all__ = [
    "FetchResult",
    "SearchHit",
    "SearxngClient",
    "Weather",
    "WeatherClient",
    "WeatherError",
    "WebFetchError",
    "WebFetcher",
    "WebSearchError",
]
