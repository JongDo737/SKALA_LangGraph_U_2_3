"""Energy-infrastructure competitor research.

The filename intentionally follows the requested `compitition.py` spelling.
It accepts a JSON object on stdin and prints a JSON object on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REQUIRED_FIELDS = ("name", "description")


@dataclass
class SearchResult:
    title: str
    snippet: str
    url: str


class SearchClient(Protocol):
    def search(self, query: str, limit: int) -> list[SearchResult]: ...


class SerperSearchClient:
    """Small dependency-free adapter for the Serper Google Search API.

    Set SERPER_API_KEY before using `--search-provider serper`.
    """

    endpoint = "https://google.serper.dev/search"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("SERPER_API_KEY")
        if not self.api_key:
            raise ValueError("SERPER_API_KEY is required for the serper provider")

    def search(self, query: str, limit: int) -> list[SearchResult]:
        body = json.dumps({"q": query, "num": limit}).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError) as exc:
            raise RuntimeError(f"Web search failed: {exc}") from exc
        return [
            SearchResult(item.get("title", ""), item.get("snippet", ""), item.get("link", ""))
            for item in payload.get("organic", [])[:limit]
        ]


class EmptySearchClient:
    """Useful when only validating data or testing the pipeline."""

    def search(self, query: str, limit: int) -> list[SearchResult]:
        return []


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def validate_companies(companies: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(companies, list):
        return [], [{"index": None, "errors": ["companies must be a list"]}]

    valid, invalid, names = [], [], set()
    for index, company in enumerate(companies):
        errors: list[str] = []
        if not isinstance(company, dict):
            invalid.append({"index": index, "errors": ["company must be an object"]})
            continue
        for key in REQUIRED_FIELDS:
            if not _clean(company.get(key)):
                errors.append(f"missing required field: {key}")
        normalized_name = _clean(company.get("name")).casefold()
        if normalized_name and normalized_name in names:
            errors.append("duplicate company name")
        names.add(normalized_name)
        if errors:
            invalid.append({"index": index, "company": company.get("name"), "errors": errors})
        else:
            valid.append(company)
    return valid, invalid


def subdomain(company: dict[str, Any]) -> str:
    """Use the upstream classification; never pretend a weak guess is certain."""
    for key in ("subdomain", "detail_domain", "sector", "domain"):
        if _clean(company.get(key)):
            return _clean(company[key])
    return "energy infrastructure (unclassified)"


def _candidate_name(result: SearchResult) -> str:
    title = re.split(r"\s[-|–:]\s", result.title, maxsplit=1)[0].strip()
    return title[:120] or result.url


def _is_same_company(name: str, candidate: str) -> bool:
    return name.casefold() in candidate.casefold() or candidate.casefold() in name.casefold()


def research_company(company: dict[str, Any], desired_count: int, client: SearchClient) -> dict[str, Any]:
    name, detail = _clean(company["name"]), subdomain(company)
    region = _clean(company.get("country") or company.get("region"))
    query = f'"{name}" competitors {detail} energy infrastructure {region}'.strip()
    results = client.search(query, max(desired_count * 3, 10))
    competitors: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in results:
        candidate = _candidate_name(item)
        key = candidate.casefold()
        if not candidate or key in seen or _is_same_company(name, candidate):
            continue
        seen.add(key)
        competitors.append({
            "name": candidate,
            "evidence": item.snippet,
            "source_url": item.url,
            "search_query": query,
        })
        if len(competitors) == desired_count:
            break
    return {
        "company": company,
        "subdomain": detail,
        "requested_competitor_count": desired_count,
        "found_competitor_count": len(competitors),
        "status": "complete" if len(competitors) == desired_count else "insufficient_search_evidence",
        "competitors": competitors,
    }


def run(payload: dict[str, Any], client: SearchClient) -> dict[str, Any]:
    companies, invalid = validate_companies(payload.get("companies"))
    # The requested rule: one target company's competitor quota equals the input company count.
    desired_count = int(payload.get("competitors_per_company") or len(companies))
    if desired_count < 0:
        raise ValueError("competitors_per_company must be zero or greater")
    results = [research_company(company, desired_count, client) for company in companies]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "domain": payload.get("domain", "energy infrastructure"),
        # judge.py가 ROIC-WACC를 계산할 수 있도록 상위 가정을 보존한다.
        "wacc": payload.get("wacc"),
        "validation": {
            "received_company_count": len(payload.get("companies", [])) if isinstance(payload.get("companies"), list) else 0,
            "valid_company_count": len(companies),
            "invalid_companies": invalid,
        },
        "research_results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="-", help="JSON file path, or - for stdin")
    parser.add_argument("--search-provider", choices=("serper", "empty"), default="empty")
    args = parser.parse_args()
    raw = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()
    payload = json.loads(raw)
    client: SearchClient = SerperSearchClient() if args.search_provider == "serper" else EmptySearchClient()
    print(json.dumps(run(payload, client), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
