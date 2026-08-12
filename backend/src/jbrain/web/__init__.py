"""On-box internet access for the jerv chatbot agent (docs/reference/ASSISTANT.md "Agent
selection"): a self-hosted SearXNG metasearch client and a URL fetch-and-extract
client. These back the `web_search` / `web_fetch` tools, which only the sandboxed
jerv agent may call.
"""

from jbrain.web.domain_health import DomainSkipRepo
from jbrain.web.favicon import FaviconFetcher, FaviconResult
from jbrain.web.federal_register import FederalRegisterClient, Notice
from jbrain.web.feeds import FeedClient, FeedItem
from jbrain.web.fetch import FetchResult, WebFetcher, WebFetchError
from jbrain.web.grokipedia import GrokipediaClient, GrokipediaError
from jbrain.web.hurricane import HurricaneClient, HurricaneError
from jbrain.web.nhc_gis import NhcGisClient, NhcGisError
from jbrain.web.nhc_surge import NhcSurgeClient, NhcSurgeError
from jbrain.web.nppes import NppesClient, Provider, Taxonomy
from jbrain.web.nws import NwsClient, NwsOutOfCoverage, NwsUnavailable
from jbrain.web.public_records import CourtListenerClient, Person, Record
from jbrain.web.search import SearchHit, SearxngClient, WebSearchError
from jbrain.web.weather import Weather, WeatherClient, WeatherError
from jbrain.web.weather_history import HistoryStats, WeatherHistoryClient
from jbrain.web.wikidata import WikidataClient, WikidataEntity

__all__ = [
    "CourtListenerClient",
    "DomainSkipRepo",
    "FaviconFetcher",
    "FaviconResult",
    "FederalRegisterClient",
    "FeedClient",
    "FeedItem",
    "FetchResult",
    "GrokipediaClient",
    "GrokipediaError",
    "HistoryStats",
    "HurricaneClient",
    "HurricaneError",
    "NhcGisClient",
    "NhcGisError",
    "NhcSurgeClient",
    "NhcSurgeError",
    "Notice",
    "NppesClient",
    "NwsClient",
    "NwsOutOfCoverage",
    "NwsUnavailable",
    "Person",
    "Provider",
    "Record",
    "SearchHit",
    "SearxngClient",
    "Taxonomy",
    "Weather",
    "WeatherClient",
    "WeatherError",
    "WeatherHistoryClient",
    "WebFetchError",
    "WebFetcher",
    "WebSearchError",
    "WikidataClient",
    "WikidataEntity",
]
