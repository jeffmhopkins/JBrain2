"""The `browse` tool's fetch-first gate: a browser only for a site web_fetch could not read.

Owner decision 2026-10-05 (docs/plans/BROWSER_AGENT_PLAN.md B1): a browse run costs a minute
or more of the box's model and a Chromium context, and jerv reached for it on pages an
ordinary fetch reads in seconds. So `browse` is refused unless, earlier in the SAME turn,
`web_fetch` of the same site came back needing a browser — a location/store picker
(`gated`), an unrendered JavaScript app (`js_shell`), or a page too thin to be the content.
The tools decide it, from the fetch result's own flags recorded on the turn's ToolContext,
never from the model's judgment or the result text: there is no "needs interaction"
override, because the agent cannot be trusted to make that call. The accepted cost: a page
that fetches fine but hides its data behind a click is refused (revisit if it bites).

"The same site" is the registrable domain (eTLD+1, from the bundled Public Suffix List), so
`www.` and other subdomains match their parent and `bbc.co.uk` is not lumped in with every
other `.co.uk` site. An address with no registrable domain (an IP, `localhost`) never passes.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from tld import get_fld

from jbrain.web.fetch import THIN_PAGE_CHARS, FetchResult

# Why a fetch said the page needs a browser, as the refusal and the trace name it.
GATED = "gated"
JS_SHELL = "js_shell"
THIN = "thin"


def registrable_domain(url: str) -> str | None:
    """`url`'s eTLD+1, lowercased; for a suffix the list does not know (`.example`, a new
    gTLD the bundled copy predates), the host itself less a leading `www.`; None for an IP,
    a dotless host or junk."""
    domain = get_fld(url.strip(), fail_silently=True)
    if isinstance(domain, str):
        return domain.lower()
    try:
        host = (urlsplit(url.strip()).hostname or "").rstrip(".")
    except ValueError:
        return None
    if "." not in host or _is_ip(host):
        return None
    return host.removeprefix("www.")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return all(label.isdigit() for label in host.split("."))
    return True


def needs_browser(result: FetchResult, *, offset: int, find: str) -> str | None:
    """Why this web_fetch result says the page needs a browser, or None. Thinness is judged
    only on a plain read from the top: a `find` or an offset window is short by design."""
    if result.gated:
        return GATED
    if result.js_shell:
        return JS_SHELL
    if offset == 0 and not find and result.total_chars < THIN_PAGE_CHARS:
        return THIN
    return None


def record_fetch(
    seen: dict[str, str], result: FetchResult, url: str, *, offset: int, find: str
) -> None:
    """Note on this turn's memo that `url`'s site needs a browser, when the fetch says so —
    under the requested URL's domain and the final (redirected) one's."""
    reason = needs_browser(result, offset=offset, find=find)
    if reason is None:
        return
    for address in (url, result.url):
        domain = registrable_domain(address)
        if domain is not None:
            seen[domain] = reason


def refusal(start_url: str | None, seen: dict[str, str]) -> str | None:
    """Why `browse` may not start at `start_url` this turn, or None when it may."""
    if not start_url:
        return (
            "browse needs a start_url: the page web_fetch could not read. web_fetch the site"
            " first; if the result says the page needs a browser (a location or store picker,"
            " a JavaScript app, or no real text), call browse with that URL as start_url."
        )
    domain = registrable_domain(start_url)
    if domain is not None and domain in seen:
        return None
    return (
        f"browse refused: web_fetch {start_url} first, this turn, and use what it returns."
        " browse is only for a site whose web_fetch result said it needs a browser (a"
        " location or store picker, a JavaScript app, or no real text); a page web_fetch can"
        " read is answered from the fetch."
    )
