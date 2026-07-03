"""On-box internet access for the jerv chatbot agent (docs/reference/ASSISTANT.md "Agent
selection"): a self-hosted SearXNG metasearch client and a URL fetch-and-extract
client. These back the `web_search` / `web_fetch` tools, which only the sandboxed
jerv agent may call.
"""

from jbrain.web.favicon import FaviconFetcher, FaviconResult
from jbrain.web.fetch import FetchResult, WebFetcher, WebFetchError
from jbrain.web.hurricane import HurricaneClient, HurricaneError
from jbrain.web.nhc_gis import NhcGisClient, NhcGisError
from jbrain.web.nhc_surge import NhcSurgeClient, NhcSurgeError
from jbrain.web.nws import NwsClient, NwsOutOfCoverage, NwsUnavailable
from jbrain.web.search import SearchHit, SearxngClient, WebSearchError
from jbrain.web.weather import Weather, WeatherClient, WeatherError

__all__ = [
    "FaviconFetcher",
    "FaviconResult",
    "FetchResult",
    "HurricaneClient",
    "HurricaneError",
    "NhcGisClient",
    "NhcGisError",
    "NhcSurgeClient",
    "NhcSurgeError",
    "NwsClient",
    "NwsOutOfCoverage",
    "NwsUnavailable",
    "SearchHit",
    "SearxngClient",
    "Weather",
    "WeatherClient",
    "WeatherError",
    "WebFetchError",
    "WebFetcher",
    "WebSearchError",
]
