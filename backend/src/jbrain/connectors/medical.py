"""The starter medical/medicine connectors (docs/reference/ASSISTANT.md "External
connectors"). Reference enrichment, not authority: results are data the agent may
cite to the owner with source attribution, never minted as facts. Both are
health-domain, free, no-auth NLM services.

- lookup_medication → NLM RxNorm/RxNav (`/REST/drugs.json?name=`): ingredients and
  related concepts for a drug name.
- lookup_condition → NLM MedlinePlus Connect (`/service`): an overview for a
  condition name or code.
"""

from typing import Any

from jbrain.connectors.base import Connector, ParamSpec

_MAX_ITEMS = 10


def parse_medication(data: Any) -> str:
    """RxNav drugGroup → a concise list of matched concepts (name + rxcui)."""
    groups = (data or {}).get("drugGroup", {}).get("conceptGroup") or []
    matches: list[str] = []
    for group in groups:
        for concept in group.get("conceptProperties") or []:
            name = concept.get("name")
            rxcui = concept.get("rxcui")
            if name:
                matches.append(f"{name} (rxcui {rxcui})" if rxcui else name)
    if not matches:
        return "No medication match found."
    lines = matches[:_MAX_ITEMS]
    return "Matches (source: NLM RxNorm/RxNav):\n" + "\n".join(f"- {m}" for m in lines)


def parse_condition(data: Any) -> str:
    """MedlinePlus Connect feed → the first entry's title and summary."""
    entries = (data or {}).get("feed", {}).get("entry") or []
    if not entries:
        return "No condition overview found."
    first = entries[0]
    title = (first.get("title") or {}).get("_value", "").strip()
    summary = (first.get("summary") or {}).get("_value", "").strip()
    out = ["Overview (source: NLM MedlinePlus):"]
    if title:
        out.append(title)
    if summary:
        out.append(summary)
    return "\n".join(out)


def medical_connectors(rxnav_url: str, medlineplus_url: str) -> list[Connector]:
    """The two health connectors, with their pinned base URLs from config."""
    return [
        Connector(
            name="lookup_medication",
            base_url=rxnav_url,
            path="/REST/drugs.json",
            domain="health",
            params=(ParamSpec("name", str),),
            parse=parse_medication,
        ),
        Connector(
            name="lookup_condition",
            base_url=medlineplus_url,
            path="/service",
            domain="health",
            params=(ParamSpec("name", str),),
            parse=parse_condition,
        ),
    ]
