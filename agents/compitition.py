# 작성자: 권태현
# 파일 설명: 10개 기업별 경쟁사를 웹에서 조사하고,
# 조사 대상 기업과 경쟁사의 주요 정보를 비교하는 기능을 담당합니다.
#
# LangGraph 연결: competition_node(state) → eligible_companies에 competition 필드 저장

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
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from prompts import format_prompt


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
    # Serper `num` 허용 범위는 보통 1~10입니다. 초과 시 400이 납니다.
    max_results = 10

    def __init__(self, api_key: str | None = None) -> None:
        raw = api_key or os.getenv("SERPER_API_KEY") or ""
        self.api_key = raw.strip().strip("\"'")
        if not self.api_key:
            raise ValueError("SERPER_API_KEY is required for the serper provider")

    def search(self, query: str, limit: int) -> list[SearchResult]:
        num = max(1, min(int(limit or 1), self.max_results))
        # 질의는 짧게 유지합니다. 과도하게 긴 description이 붙으면 400/품질 저하 원인이 됩니다.
        safe_query = re.sub(r"\s+", " ", query).strip()[:240]
        body = json.dumps({"q": safe_query, "num": num}).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            headers={
                "X-API-KEY": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")[:300]
            except Exception:
                detail = str(exc)
            print(
                f"  [competition] Serper 검색 실패 ({exc.code}): {detail or exc.reason}. "
                "빈 결과로 계속 진행합니다."
            )
            return []
        except URLError as exc:
            print(f"  [competition] Serper 네트워크 오류: {exc}. 빈 결과로 계속 진행합니다.")
            return []
        return [
            SearchResult(
                item.get("title", ""),
                item.get("snippet", ""),
                item.get("link", ""),
            )
            for item in payload.get("organic", [])[:num]
        ]


class EmptySearchClient:
    """Useful when only validating data or testing the pipeline."""

    def search(self, query: str, limit: int) -> list[SearchResult]:
        return []


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_placeholder_api_key(value: str) -> bool:
    cleaned = value.strip().strip("\"'").casefold()
    return (
        not cleaned
        or cleaned.startswith("your_")
        or cleaned in {"changeme", "todo", "xxx", "test"}
    )


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
    region = _clean(company.get("country") or company.get("region")) or "KR"
    # 긴 description/재무 문구를 넣지 않고, 경쟁사 검색에 필요한 짧은 질의만 사용합니다.
    short_detail = re.split(r"[|/]", detail)[0].strip()[:60]
    query = format_prompt(
        "competition_search",
        company_name=name,
        detail=short_detail,
    ).strip()
    search_limit = max(desired_count * 3, 10) if desired_count > 0 else 10

    try:
        results = client.search(query, search_limit)
    except Exception as error:
        print(f"  [competition] '{name}' 검색 예외: {error}. 빈 결과로 계속합니다.")
        results = []
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
        if desired_count > 0 and len(competitors) == desired_count:
            break
    complete = desired_count == 0 or len(competitors) >= desired_count
    return {
        "company": company,
        "subdomain": detail,
        "requested_competitor_count": desired_count,
        "found_competitor_count": len(competitors),
        "status": "complete" if complete else "insufficient_search_evidence",
        "competitors": competitors,
    }


def run(payload: dict[str, Any], client: SearchClient) -> dict[str, Any]:
    companies, invalid = validate_companies(payload.get("companies"))
    # The requested rule: one target company's competitor quota equals the input company count.
    raw_desired = payload.get("competitors_per_company")
    desired_count = len(companies) if raw_desired is None else int(raw_desired)
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


# ---------------------------------------------------------------------------
# LangGraph 노드
# ---------------------------------------------------------------------------
def build_search_client() -> SearchClient:
    """SERPER_API_KEY가 유효하면 Serper, 아니면 empty 클라이언트를 씁니다."""

    key = (os.getenv("SERPER_API_KEY") or "").strip().strip("\"'")
    if _is_placeholder_api_key(key):
        print("  [competition] SERPER_API_KEY 없음/예시값 → EmptySearchClient 사용")
        return EmptySearchClient()
    try:
        return SerperSearchClient(key)
    except ValueError as error:
        print(f"  [competition] Serper 비활성: {error}")
        return EmptySearchClient()


def competition_node(state: dict[str, Any]) -> dict[str, Any]:
    """State의 시장성(enrich) 기업으로 경쟁사 조사를 수행합니다.

    이미 투자 적합 판단을 받은 기업은 기존 경쟁사 조사를 유지하고,
    신규·미판단 기업만 웹 검색합니다.
    """

    from copy import deepcopy

    attempt = int(state.get("competition_attempts", 0)) + 1
    input_companies = list(state.get("eligible_companies", []))

    reuse: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for company in input_companies:
        judgement = company.get("judgement") if isinstance(company.get("judgement"), dict) else {}
        research = company.get("competitor_research") or company.get("competition")
        if judgement.get("decision") == "적합" and isinstance(research, dict):
            reuse.append(deepcopy(company))
        else:
            pending.append(company)

    companies_payload = []
    for company in pending:
        market = company.get("market") if isinstance(company.get("market"), dict) else {}
        market_context = (
            company.get("market_context")
            if isinstance(company.get("market_context"), dict)
            else {}
        )
        inferred = (
            market_context.get("inferred")
            if isinstance(market_context.get("inferred"), dict)
            else {}
        )
        summary = (
            market_context.get("summary")
            if isinstance(market_context.get("summary"), dict)
            else {}
        )
        market_summary = (
            market.get("summary") if isinstance(market.get("summary"), dict) else {}
        )
        description = (
            company.get("description")
            or market.get("description")
            or inferred.get("description")
            or company.get("intro")
            or ""
        )
        # summary 가 있으면 짧은 시장 문맥을 description 뒤에 보강 (검색 질의는 name+subdomain만 씀)
        market_blurb_parts = [
            (market_summary or summary).get("target_market"),
            (market_summary or summary).get("market_size"),
        ]
        market_blurb = " / ".join(part for part in market_blurb_parts if part)
        if market_blurb and market_blurb not in description:
            description = f"{description} | {market_blurb}".strip(" |")

        companies_payload.append(
            {
                "name": company.get("name") or company.get("company_name"),
                "description": description,
                "subdomain": (
                    company.get("subdomain")
                    or market.get("subdomain")
                    or inferred.get("subdomain")
                    or "energy infrastructure"
                ),
                "country": (
                    company.get("country")
                    or company.get("region")
                    or inferred.get("country")
                    or "KR"
                ),
                "financials": company.get("financials")
                or (
                    [company.get("financial_summary")]
                    if isinstance(company.get("financial_summary"), dict)
                    else []
                ),
                "wacc": company.get("wacc"),
                "_state_id": company.get("id") or company.get("corp_code"),
            }
        )

    client = build_search_client()
    try:
        from config import DEFAULT_WACC
    except ImportError:
        DEFAULT_WACC = 0.10
    state_wacc = state.get("wacc")
    if state_wacc is None:
        state_wacc = DEFAULT_WACC
    desired = (
        0
        if isinstance(client, EmptySearchClient)
        else max(len(companies_payload) or len(input_companies), 1)
    )
    payload = {
        "domain": state.get("domain_keywords") or "energy infrastructure",
        "wacc": state_wacc,
        "companies": companies_payload,
        "competitors_per_company": desired,
    }
    # 경쟁사 조사 대상 기업 payload에 wacc를 명시적으로 심습니다.
    for row in companies_payload:
        row["wacc"] = row.get("wacc") or state_wacc
    result = (
        run(payload, client)
        if companies_payload
        else {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "domain": payload["domain"],
            "wacc": payload["wacc"],
            "validation": {
                "received_company_count": 0,
                "valid_company_count": 0,
                "invalid_companies": [],
            },
            "research_results": [],
        }
    )

    by_name = {
        str((item.get("company") or {}).get("name") or "").casefold(): item
        for item in result.get("research_results", [])
    }
    enriched: list[dict[str, Any]] = []
    for company in input_companies:
        name = str(company.get("name") or company.get("company_name") or "").casefold()
        updated = deepcopy(company)
        judgement = updated.get("judgement") if isinstance(updated.get("judgement"), dict) else {}
        existing = updated.get("competitor_research") or updated.get("competition")
        if judgement.get("decision") == "적합" and isinstance(existing, dict):
            updated["competition"] = existing
            updated["competitor_research"] = existing
        else:
            research = by_name.get(name)
            updated["competition"] = research or {
                "status": "missing",
                "competitors": [],
                "found_competitor_count": 0,
                "requested_competitor_count": payload["competitors_per_company"],
            }
            updated["competitor_research"] = updated["competition"]
        enriched.append(updated)

    # judge가 전체 기업 research_results를 쓸 수 있도록 재사용분도 payload에 합칩니다.
    merged_research = list(result.get("research_results", []))
    for company in reuse:
        research = company.get("competitor_research") or company.get("competition")
        if isinstance(research, dict):
            merged_research.append(research)
    result = {**result, "research_results": merged_research}

    names = [c.get("name") or c.get("company_name") for c in enriched]
    message = (
        f"{attempt}차 경쟁사 비교: 신규 {len(pending)}개 조사, "
        f"기존 적합 유지 {len(reuse)}개 "
        f"(유효={result.get('validation', {}).get('valid_company_count', 0)})"
    )
    print("\n[작업] 경쟁사 조사")
    print(f"  입력 State : 기업={names}")
    print(
        f"  반환 값    : research_results={len(result.get('research_results', []))}개, "
        f"invalid={result.get('validation', {}).get('invalid_companies', [])}"
    )
    for item in result.get("research_results", []):
        company = item.get("company") or {}
        competitors = item.get("competitors") if isinstance(item.get("competitors"), list) else []
        competitor_names = [
            str(row.get("name") or "").strip()
            for row in competitors
            if isinstance(row, dict) and str(row.get("name") or "").strip()
        ]
        print(
            f"  · {company.get('name')}: status={item.get('status')}, "
            f"경쟁사={item.get('found_competitor_count')}/"
            f"{item.get('requested_competitor_count')}, "
            f"names={competitor_names}"
        )
        for index, row in enumerate(competitors[:5], 1):
            if not isinstance(row, dict):
                continue
            print(
                f"      [{index}] {row.get('name')} | "
                f"{(row.get('evidence') or '')[:80]} | {row.get('source_url')}"
            )

    return {
        "eligible_companies": enriched,
        "competition_attempts": attempt,
        "competition_payload": result,
        "wacc": state_wacc,
        "next_stage": "judge_investment",
        "execution_log": [*state.get("execution_log", []), message],
    }
