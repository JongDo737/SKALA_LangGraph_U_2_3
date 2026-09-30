# 작성자: 권태현
# 파일 설명: 기업별 경쟁사를 OpenAI LLM으로 추천하고,
# 보고서 참고용 competitor_data(사업·제품·기술 요약)를 제공합니다.
# (웹 검색 모드는 COMPETITION_PROVIDER=web 일 때만 사용)
#
# LangGraph 연결: competition_node(state) → eligible_companies에 competition 필드 저장

"""Energy-infrastructure competitor research (LLM 기본).

The filename intentionally follows the requested `compitition.py` spelling.
It accepts a JSON object on stdin and prints a JSON object on stdout.
기본 경로는 OpenAI 추천이며, 웹·DART 검증은 수행하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from prompts import format_prompt


REQUIRED_FIELDS = ("name", "description")
COMPETITION_PROVIDER = (os.getenv("COMPETITION_PROVIDER") or "openai").strip().lower()
COMPETITION_MODEL = os.getenv("COMPETITION_MODEL") or os.getenv(
    "OPENAI_MODEL", "gpt-4o-mini"
)


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
            with urlopen(request, timeout=int(os.getenv("SERPER_TIMEOUT", "60"))) as response:
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
        except (TimeoutError, URLError) as exc:
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


def _normalized_company_name(value: str) -> str:
    """Normalise legal suffixes so identity checks work across Korean spellings."""
    value = value.casefold()
    # 긴 법인 표기를 먼저 제거한다. 짧은 '주'를 먼저 처리하면 '주식회사'가
    # '식회사'로 남아 동일 법인 비교가 실패할 수 있다.
    value = re.sub(r"주식회사|유한회사|합자회사|합명회사|\(?주\)?", "", value)
    return re.sub(r"[^0-9a-z가-힣]", "", value)


def _is_same_company(name: str, candidate: str) -> bool:
    left, right = _normalized_company_name(name), _normalized_company_name(candidate)
    return bool(left and right and (left == right or left in right or right in left))


def _url_host(url: str) -> str:
    return urlsplit(url).netloc.casefold().removeprefix("www.")


def _site_key(host: str) -> str:
    """Collapse mobile/subdomain variants so one site cannot count twice."""
    parts = [part for part in host.split(".") if part]
    if len(parts) < 2:
        return host
    # co.kr, or.kr 같은 국내 2단계 최상위 도메인은 등록 도메인까지 보존한다.
    if len(parts) >= 3 and parts[-2] in {"co", "or", "go", "ac", "ne", "re", "pe"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


_NON_COMPANY_TITLE = re.compile(
    r"기업정보|채용|사람인|잡코리아|ceo\s*message|top\s*\d+|ultimate\s+winner|"
    r"뉴스|기사|영상|youtube|how\s+to|후기|리뷰|순위|비교|보고서|논문|연구|시장|업체|제조|투자|움직임|"
    r"에너지\s*회사|전력\s*생산업체|최대\s*규모|실적|엑스포|법적\s*리스크|"
    r"etf|fund|펀드|scouting|주가|stock|ticker|지수",
    re.IGNORECASE,
)
def _looks_like_company_name(candidate: str, result: SearchResult) -> bool:
    """Reject page/article titles before treating them as corporate entities.

    A candidate may come from a news article or an industry page; it does not
    have to be an official corporate site.  Those pages are therefore not
    rejected merely because of their host.  They must still pass the separate
    two-source identity check below.
    """
    if (
        len(_normalized_company_name(candidate)) < 2
        or _NON_COMPANY_TITLE.search(candidate)
        or ("," in candidate and len(candidate) > 24)
    ):
        return False
    host = _url_host(result.url)
    return bool(host) and not urlsplit(result.url).path.casefold().endswith(".pdf")


def _web_identity_evidence(candidate: str, target_name: str, result: SearchResult) -> dict[str, str] | None:
    """Return usable web evidence only when it names the candidate itself."""
    host = _url_host(result.url)
    text = f"{result.title} {result.snippet}"
    candidate_key = _normalized_company_name(candidate)
    text_key = _normalized_company_name(text)
    if (
        not host
        or urlsplit(result.url).path.casefold().endswith(".pdf")
        or not candidate_key
        or candidate_key not in text_key
        or _is_same_company(target_name, text)
    ):
        return None
    return {
        "title": result.title,
        "snippet": result.snippet,
        "url": result.url,
        "host": host,
        "site_key": _site_key(host),
    }


def _verify_web_company(
    target_name: str,
    candidate: str,
    result: SearchResult,
    detail: str,
    client: SearchClient,
) -> tuple[bool, str, list[dict[str, str]], str]:
    """Cross-check a candidate name on two independent web domains.

    Official sites are useful but deliberately not required.  The point is to
    keep a paper, list, or article title from becoming ``competitors[].name``.
    """
    if not _looks_like_company_name(candidate, result):
        return False, "회사명이 아닌 문서·기사·목록 제목입니다.", [], ""
    if _is_same_company(target_name, candidate):
        return False, "대상 회사와 동일한 이름입니다.", [], ""

    verification_query = f'"{candidate}" 기업 {detail[:40]}'.strip()
    evidence: list[dict[str, str]] = []
    primary = _web_identity_evidence(candidate, target_name, result)
    if primary:
        evidence.append(primary)
    try:
        verification_results = client.search(verification_query, 5)
    except Exception as error:
        return False, f"웹 교차 검증 검색 실패: {error}", evidence, verification_query

    seen_sites = {row["site_key"] for row in evidence}
    for verified in verification_results:
        row = _web_identity_evidence(candidate, target_name, verified)
        if row and row["site_key"] not in seen_sites:
            evidence.append(row)
            seen_sites.add(row["site_key"])
        if len(evidence) >= 2:
            break
    if len(evidence) < 2:
        return False, "서로 다른 웹 출처 2곳에서 회사명을 확인하지 못했습니다.", evidence, verification_query
    for row in evidence:
        row.pop("site_key", None)
    return True, "서로 다른 웹 출처 2곳에서 별도 회사명을 확인했습니다.", evidence[:2], verification_query


def collect_technical_comparison(
    company: dict[str, Any],
    peer: dict[str, Any],
    client: SearchClient | None = None,
) -> dict[str, Any]:
    """경쟁사 제품·기술 비교 근거를 정리합니다. LLM 추천 시 competitor_data를 우선합니다."""

    peer_name = str(peer.get("name") or "")
    competitor_data = (
        peer.get("competitor_data") if isinstance(peer.get("competitor_data"), dict) else {}
    )
    if competitor_data:
        products = competitor_data.get("products_services") or []
        techs = competitor_data.get("technologies_capabilities") or []
        return {
            "status": "evidence_collected",
            "reason": "OpenAI 추천 competitor_data 기반 참고 비교입니다. 웹·DART 검증은 없습니다.",
            "query": None,
            "comparison_dimensions": ["제품·서비스", "기술 차별점", "고객·프로젝트"],
            "sources": [
                {
                    "title": peer_name,
                    "snippet": str(
                        competitor_data.get("competitive_relevance")
                        or peer.get("evidence")
                        or ""
                    ),
                    "url": "",
                }
            ],
            "products_services": products if isinstance(products, list) else [],
            "technologies_capabilities": techs if isinstance(techs, list) else [],
            "data_status": competitor_data.get("data_status") or "ai_generated_unverified",
        }

    query = f'"{company.get("name")}" "{peer_name}" 기술 제품 서비스 경쟁'.strip()
    sources: list[dict[str, str]] = []
    if peer.get("source_url") or peer.get("evidence"):
        sources.append(
            {
                "title": peer_name,
                "snippet": str(peer.get("evidence") or ""),
                "url": str(peer.get("source_url") or ""),
            }
        )
    if client is None:
        return {
            "status": "evidence_collected" if sources else "unavailable",
            "reason": "경쟁사 근거로 비교를 진행합니다."
            if sources
            else "기술 비교 근거가 없습니다.",
            "query": query,
            "sources": sources,
        }
    try:
        results = client.search(query, 5)
    except Exception as error:
        return {
            "status": "evidence_collected" if sources else "unavailable",
            "reason": "경쟁사 선정 웹 근거로 비교를 진행합니다."
            if sources
            else f"기술 비교 검색 실패: {error}",
            "query": query,
            "sources": sources,
        }
    sources.extend(
        {"title": row.title, "snippet": row.snippet, "url": row.url} for row in results
    )
    return {
        "status": "evidence_collected" if sources else "insufficient_evidence",
        "reason": "웹 근거를 수집했습니다. 우위 판단은 근거 검토가 필요합니다."
        if sources
        else "제품·기술 비교를 위한 웹 근거가 부족합니다.",
        "query": query,
        "comparison_dimensions": [
            "제품·서비스",
            "기술 차별점",
            "고객·프로젝트",
            "인증·파트너십",
        ],
        "sources": sources,
    }


def _normalize_competitor_data(raw: Any) -> dict[str, Any]:
    data = dict(raw) if isinstance(raw, dict) else {}

    def _str_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    return {
        "business_summary": str(data.get("business_summary") or "").strip(),
        "products_services": _str_list(data.get("products_services")),
        "technologies_capabilities": _str_list(data.get("technologies_capabilities")),
        "target_customers": _str_list(data.get("target_customers")),
        "competitive_relevance": str(data.get("competitive_relevance") or "").strip(),
        "data_status": "ai_generated_unverified",
        "data_notice": (
            data.get("data_notice")
            or "웹·DART 조회 없이 OpenAI 지식 기반으로 생성된 보고서 참고 정보입니다."
        ),
    }


def recommend_competitors_with_openai(
    company: dict[str, Any],
    desired_count: int,
    *,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """OpenAI로 경쟁사 후보와 competitor_data를 생성합니다."""

    from openai import OpenAI

    name = _clean(company.get("name"))
    detail = subdomain(company)
    description = _clean(company.get("description")) or detail
    country = _clean(company.get("country")) or "KR"
    count = max(1, int(desired_count or 1))
    prompt = format_prompt(
        "competition_recommend",
        count=count,
        company_name=name,
        description=description,
        subdomain=detail,
        country=country,
    )
    client = OpenAI()
    response = client.chat.completions.create(
        model=model or COMPETITION_MODEL,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": "당신은 에너지 인프라 경쟁사 조사 도우미입니다. 반드시 JSON만 반환하세요.",
            },
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or "{}"
    payload = json.loads(content)
    rows = payload.get("competitors") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []

    competitors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        candidate = _clean(item.get("name"))
        if not candidate or _is_same_company(name, candidate):
            continue
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        competitors.append(
            {
                "name": candidate,
                "evidence": _clean(item.get("evidence"))
                or "사업모델이 유사하여 직접 경쟁 가능성이 높음.",
                "competitor_data": _normalize_competitor_data(item.get("competitor_data")),
                "source_url": None,
                "search_query": None,
                "identity_verification": {
                    "status": "ai_recommended_unverified",
                    "reason": "OpenAI의 사업 설명 기반 추천입니다. 웹·DART 검증은 수행하지 않았습니다.",
                    "method": "openai_recommendation",
                },
            }
        )
        if len(competitors) >= count:
            break
    return competitors


def research_company_openai(
    company: dict[str, Any],
    desired_count: int,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """OpenAI 추천 기반 경쟁사 조사 결과(요청 스키마)를 반환합니다."""

    detail = subdomain(company)
    count = max(1, int(desired_count or 1))
    try:
        competitors = recommend_competitors_with_openai(company, count, model=model)
    except Exception as error:
        print(f"  [competition] OpenAI 추천 실패 ({company.get('name')}): {error}")
        competitors = []
    complete = len(competitors) >= count
    return {
        "company": {
            "name": company.get("name"),
            "description": company.get("description"),
            "subdomain": detail,
            "country": company.get("country") or "KR",
        },
        "subdomain": detail,
        "requested_competitor_count": count,
        "found_competitor_count": len(competitors),
        "status": "complete" if complete else "insufficient_ai_evidence",
        "competitors": competitors,
        "identity_rejected_candidates": [],
        "selection_method": "openai_recommendation",
    }


def research_company(
    company: dict[str, Any],
    desired_count: int,
    client: SearchClient | None = None,
) -> dict[str, Any]:
    """기본은 OpenAI 추천. COMPETITION_PROVIDER=web 이면 웹 검색 경로를 사용합니다."""

    provider = (os.getenv("COMPETITION_PROVIDER") or COMPETITION_PROVIDER or "openai").lower()
    if provider in {"openai", "llm", "ai"} or client is None:
        return research_company_openai(company, desired_count)

    name, detail = _clean(company["name"]), subdomain(company)
    description = _clean(company.get("description"))
    search_topic = description or detail
    search_topic = re.sub(r"\s*(?:사업|업)\s*$", "", search_topic).strip() or search_topic
    short_detail = re.split(r"[|/]", search_topic)[0].strip()[:80]
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
    competitors: list[dict[str, Any]] = []
    identity_rejected_candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in results:
        candidate = _candidate_name(item)
        key = candidate.casefold()
        if not candidate or key in seen:
            continue
        seen.add(key)
        verified, identity_reason, verification_sources, verification_query = (
            _verify_web_company(name, candidate, item, short_detail, client)
        )
        if not verified:
            identity_rejected_candidates.append(
                {"name": candidate, "reason": identity_reason, "url": item.url}
            )
            continue
        competitors.append(
            {
                "name": candidate,
                "evidence": item.snippet,
                "source_url": item.url,
                "search_query": query,
                "identity_verification": {
                    "status": "web_cross_verified",
                    "reason": identity_reason,
                    "method": "서로 다른 웹 도메인 2곳에서 회사명 확인",
                    "verification_query": verification_query,
                    "verification_sources": verification_sources,
                },
            }
        )
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
        "identity_rejected_candidates": identity_rejected_candidates,
        "selection_method": "web_search",
    }


def run(payload: dict[str, Any], client: SearchClient | None = None) -> dict[str, Any]:
    companies, invalid = validate_companies(payload.get("companies"))
    raw_desired = payload.get("competitors_per_company")
    desired_count = 1 if raw_desired is None else int(raw_desired)
    if desired_count < 0:
        raise ValueError("competitors_per_company must be zero or greater")
    results = []
    for company in companies:
        research = research_company(company, desired_count, client)
        peer = (research.get("competitors") or [None])[0]
        technical = (
            collect_technical_comparison(company, peer, client)
            if peer
            else {
                "status": "unavailable",
                "reason": "비교할 경쟁사를 찾지 못했습니다.",
                "sources": [],
            }
        )
        results.append({**research, "technical_comparison": technical})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "domain": payload.get("domain", "energy infrastructure"),
        "wacc": payload.get("wacc"),
        "validation": {
            "received_company_count": len(payload.get("companies", []))
            if isinstance(payload.get("companies"), list)
            else 0,
            "valid_company_count": len(companies),
            "invalid_companies": invalid,
        },
        "research_results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="-", help="JSON file path, or - for stdin")
    parser.add_argument(
        "--provider",
        choices=("openai", "web", "serper", "empty"),
        default=None,
        help="openai(기본) 또는 web/serper",
    )
    args = parser.parse_args()
    raw = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()
    payload = json.loads(raw)
    provider = (args.provider or COMPETITION_PROVIDER).lower()
    if provider in {"web", "serper"}:
        os.environ["COMPETITION_PROVIDER"] = "web"
        client: SearchClient | None = SerperSearchClient()
    elif provider == "empty":
        os.environ["COMPETITION_PROVIDER"] = "web"
        client = EmptySearchClient()
    else:
        os.environ["COMPETITION_PROVIDER"] = "openai"
        client = None
    print(json.dumps(run(payload, client), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# LangGraph 노드
# ---------------------------------------------------------------------------
def build_search_client() -> SearchClient:
    """웹 모드에서만 사용. SERPER_API_KEY가 유효하면 Serper, 아니면 empty."""

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

    기본은 OpenAI LLM 추천입니다. 이미 투자 적합 판단을 받은 기업은
    기존 경쟁사 조사를 유지하고, 신규·미판단 기업만 조사합니다.
    """

    from copy import deepcopy

    attempt = int(state.get("competition_attempts", 0)) + 1
    input_companies = list(state.get("eligible_companies", []))

    reuse: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for company in input_companies:
        judgement = (
            company.get("judgement") if isinstance(company.get("judgement"), dict) else {}
        )
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
        companies_payload.append(
            {
                "name": company.get("name") or company.get("company_name"),
                "description": (
                    company.get("description")
                    or market.get("description")
                    or inferred.get("description")
                    or company.get("intro")
                    or ""
                ),
                "subdomain": (
                    company.get("subdomain")
                    or market.get("subdomain")
                    or inferred.get("subdomain")
                    or "energy infrastructure"
                ),
                "country": company.get("country")
                or company.get("region")
                or inferred.get("country")
                or "KR",
                "financials": company.get("financials")
                or (
                    [company.get("financial_summary")]
                    if isinstance(company.get("financial_summary"), dict)
                    else []
                ),
                "wacc": company.get("wacc"),
                "corp_code": company.get("corp_code"),
                "_state_id": company.get("id") or company.get("corp_code"),
            }
        )

    try:
        from config import DEFAULT_WACC
    except ImportError:
        DEFAULT_WACC = 0.10
    state_wacc = state.get("wacc")
    if state_wacc is None:
        state_wacc = DEFAULT_WACC
    desired = 1
    payload = {
        "domain": state.get("domain_keywords") or "energy infrastructure",
        "wacc": state_wacc,
        "companies": companies_payload,
        "competitors_per_company": desired,
    }
    for row in companies_payload:
        row["wacc"] = row.get("wacc") or state_wacc

    provider = (os.getenv("COMPETITION_PROVIDER") or COMPETITION_PROVIDER or "openai").lower()
    use_llm = provider in {"openai", "llm", "ai", ""}
    client = None if use_llm else build_search_client()
    print(
        f"\n[작업] 경쟁사 비교 "
        f"(provider={'openai' if use_llm else 'web'}, "
        f"model={COMPETITION_MODEL if use_llm else '-'})"
    )
    if companies_payload:
        result = run(payload, client)
    else:
        result = {
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

    by_name = {
        str((item.get("company") or {}).get("name") or "").casefold(): item
        for item in result.get("research_results", [])
    }
    enriched: list[dict[str, Any]] = []
    for company in input_companies:
        name = str(company.get("name") or company.get("company_name") or "").casefold()
        updated = deepcopy(company)
        judgement = (
            updated.get("judgement") if isinstance(updated.get("judgement"), dict) else {}
        )
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
                "selection_method": "openai_recommendation" if use_llm else "web_search",
            }
            updated["competitor_research"] = updated["competition"]
            if research and isinstance(research.get("technical_comparison"), dict):
                updated["technical_comparison"] = research["technical_comparison"]
        enriched.append(updated)

    merged_research = list(result.get("research_results", []))
    for company in reuse:
        research = company.get("competitor_research") or company.get("competition")
        if isinstance(research, dict):
            merged_research.append(research)
    result = {**result, "research_results": merged_research}

    names = [c.get("name") or c.get("company_name") for c in enriched]
    message = (
        f"{attempt}차 경쟁사 비교: 신규 {len(pending)}개 조사 "
        f"({'OpenAI' if use_llm else '웹'}), "
        f"기존 적합 유지 {len(reuse)}개 "
        f"(유효={result.get('validation', {}).get('valid_company_count', 0)})"
    )
    print(f"  입력 State : 기업={names}")
    print(
        f"  반환 값    : research_results={len(result.get('research_results', []))}개, "
        f"invalid={result.get('validation', {}).get('invalid_companies', [])}"
    )
    for item in result.get("research_results", []):
        company = item.get("company") or {}
        competitors = (
            item.get("competitors") if isinstance(item.get("competitors"), list) else []
        )
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
                f"{(row.get('evidence') or '')[:80]} | "
                f"{(row.get('identity_verification') or {}).get('method')}"
            )

    return {
        "eligible_companies": enriched,
        "competition_attempts": attempt,
        "competition_payload": result,
        "wacc": state_wacc,
        "next_stage": "judge_investment",
        "execution_log": [*state.get("execution_log", []), message],
    }
