"""검증·근거 충분성·보고서 품질 판정 규칙 (순수 함수, LLM·네트워크 없음).

- summarize_*       : 워커 payload → State에 올릴 작은 요약
- validate_payload  : 워커 결과가 형식상 유효한가 (완료 결과 검증)
- check_company     : 필수 근거가 충분한가 (필수 근거 충분성 검사)
- directive_for     : 실패 코드 → 재작업 시 워커에게 줄 지시
- check_report      : 보고서 품질 3항목 (Groundedness / 중립성 / 편향 통제)
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from orchestrator.settings import Settings

LOWQ_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "blog.naver.com",
    "tistory.com",
    "brunch.co.kr",
)
INTERNAL_DOMAIN = "internal-document-pool"


def domain_of(url: str | None, fallback: str = INTERNAL_DOMAIN) -> str:
    if not url:
        return fallback
    host = urlparse(url).netloc.lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    return host or fallback


def is_lowquality(url: str | None) -> bool:
    host = domain_of(url, "")
    return any(host == d or host.endswith("." + d) or d in host for d in LOWQ_DOMAINS)


# ═════════════════════════════════════════════════════════════
# 요약 (State에 올리는 작은 값)
# ═════════════════════════════════════════════════════════════
def has_financial_data(record: dict[str, Any]) -> bool:
    """재무 값이 하나라도 있는가 (financial_summary 또는 연도별 financials)."""

    summary = record.get("financial_summary") or {}
    if any(v not in (None, "", 0) for v in summary.values()):
        return True
    return any(
        any(v not in (None, "", 0) for k, v in row.items() if k not in ("year", "사업연도"))
        for row in (record.get("financials") or [])
        if isinstance(row, dict)
    )


def fin_coverage(record: dict[str, Any]) -> float:
    """재무 워커와 같은 계산으로 VBM 지표 중 계산 가능한 비율을 구합니다. (스크리닝 순위용 — 계산만 하므로 즉시 끝남)

    수집된 재무 키 개수와 달리, 지표를 실제로 계산할 수 있는지를 봅니다(예: 키 11/14개인데도 계산 가능 지표는 33%인 기업이 있음).
    """
    try:
        from agents.financial_vbm import run_financial_agent
        from config import DEFAULT_WACC

        company = {
            "name": record.get("name", ""),
            "financials": record.get("financials") or [record.get("financial_summary") or {}],
            "wacc": DEFAULT_WACC,
        }
        return float(run_financial_agent(company, DEFAULT_WACC)["vbm"].get("data_coverage") or 0.0)
    except Exception:  # noqa: BLE001 - 계산이 안 되는 기업은 순위만 뒤로 밀립니다
        return 0.0


def summarize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    sources = profile.get("sources") or []
    return {
        "n_sources": len([s for s in sources if s.get("url")]),
        "desc_chars": len((profile.get("description") or "").strip()),
        "subdomain": profile.get("subdomain") or "",
        "method": profile.get("method") or "",
    }


def summarize_financial(payload: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    vbm = payload.get("vbm") or {}
    return {
        "status": vbm.get("status"),
        "conclusion": vbm.get("conclusion"),
        "coverage": vbm.get("data_coverage"),
        "acceptable": bool(payload.get("acceptable")),
    }


def summarize_market_rag(payload: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    market = payload.get("market") or {}
    summary = market.get("summary") or {}
    return {
        "status": market.get("status"),
        "n_sources": len(market.get("sources") or []),
        "has_size": bool(summary.get("market_size")),
        "has_growth": bool(summary.get("growth")),
    }


def summarize_market_score(payload: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    assessment = payload.get("assessment") or {}
    questions = assessment.get("questions") or []
    evidence = [e for q in questions for e in (q.get("evidence") or [])]
    domains = {domain_of(e.get("url"), "") for e in evidence if e.get("url")}
    lowq = sum(1 for e in evidence if is_lowquality(e.get("url")))
    return {
        "score": assessment.get("score"),
        "n_questions": len(questions),
        "n_scored": sum(1 for q in questions if q.get("score") is not None),
        "n_without_evidence": sum(1 for q in questions if not q.get("evidence")),
        "n_evidence": len(evidence),
        "n_domains": len(domains),
        "lowq_share": round(lowq / len(evidence), 3) if evidence else 1.0,
    }


GENERIC_COMPETITOR_NAMES = ("사업소개", "회사소개", "기업소개", "회사 소개", "about", "소개")
# 페이지 제목에서 잘못 뽑힌 일반 명사(회사 이름이 아님). 이름의 모든 단어가 이 집합에 속하면 경쟁사로 보지 않습니다.
GENERIC_TOKENS = frozenset(
    "태양광 태양열 시공 기술 에너지 사업 소개 회사 기업 발전 발전소 신재생 신재생에너지 전력 시장 서비스 제품 솔루션 개요 "
    "홈 문의 채용 뉴스 설치 운영 유지보수 관리 효율 친환경 프로젝트 사례 about home solar energy technology company "
    "business service services solution solutions project projects news".split()
)


def _is_generic_name(name: str) -> bool:
    tokens = [t for t in re.split(r"[\s·,/|:()\-–—]+", name.lower()) if t]
    return len(name.strip()) <= 2 or not tokens or all(t in GENERIC_TOKENS for t in tokens)


def _core_name(name: str) -> str:
    return re.sub(r"주식회사|\(주\)|㈜|\s", "", name or "")


def is_self_competitor(competitor: dict[str, Any], profile: dict[str, Any] | None) -> bool:
    """경쟁사로 기업 자신(의 홈페이지·소개 페이지)이 잡혔는가."""

    if not profile:
        return False
    name = (competitor.get("name") or "").strip()
    url = competitor.get("source_url") or ""
    own_urls = {s.get("url") for s in profile.get("sources") or [] if s.get("url")}
    core = _core_name(profile.get("name", ""))
    return (
        not name
        or _is_generic_name(name)
        or name.lower() in GENERIC_COMPETITOR_NAMES
        or (bool(url) and url in own_urls)
        or (len(core) >= 3 and core in _core_name(name))
    )


def summarize_competitor(payload: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    research = payload.get("research") or {}
    competitors = [c for c in (research.get("competitors") or []) if isinstance(c, dict)]
    self_ref = sum(1 for c in competitors if is_self_competitor(c, profile))
    grounded = sum(
        1 for c in competitors if c.get("source_url") and not is_self_competitor(c, profile)
    )
    return {
        "found": len(competitors),
        "with_url": sum(1 for c in competitors if c.get("source_url")),
        "grounded": grounded,
        "self_ref": self_ref,
        "with_evidence": sum(1 for c in competitors if (c.get("evidence") or "").strip()),
        "method": research.get("selection_method") or "",
    }


SUMMARIZERS = {
    "financial": summarize_financial,
    "market_rag": summarize_market_rag,
    "market_score": summarize_market_score,
    "competitor": summarize_competitor,
}


# ═════════════════════════════════════════════════════════════
# 완료 결과 검증 (형식)
# ═════════════════════════════════════════════════════════════
def validate_payload(kind: str, payload: Any) -> list[str]:
    """형식 문제 목록. 비어 있으면 유효합니다."""

    if not isinstance(payload, dict):
        return ["payload가 dict가 아닙니다"]
    if kind == "financial":
        vbm = payload.get("vbm")
        if not isinstance(vbm, dict) or "metrics" not in vbm:
            return ["vbm.metrics 가 없습니다"]
    elif kind == "market_rag":
        if not isinstance(payload.get("market"), dict):
            return ["market 객체가 없습니다"]
    elif kind == "market_score":
        assessment = payload.get("assessment")
        if not isinstance(assessment, dict) or not isinstance(
            assessment.get("questions"), list
        ):
            return ["assessment.questions 가 없습니다"]
    elif kind == "competitor":
        research = payload.get("research")
        if not isinstance(research, dict) or not isinstance(
            research.get("competitors"), list
        ):
            return ["research.competitors 가 없습니다"]
    return []


# ═════════════════════════════════════════════════════════════
# 필수 근거 충분성 검사
# ═════════════════════════════════════════════════════════════
def _fail(company_id, kind, code, reason, needed):
    return {
        "stage": "sufficiency",
        "company_id": company_id,
        "kind": kind,
        "code": code,
        "reason": reason,
        "needed": needed,
    }


def check_company(
    company_id: str,
    profile_summary: dict[str, Any],
    summaries: dict[str, dict[str, Any]],
    settings: Settings,
) -> list[dict[str, Any]]:
    """기업 1곳의 근거 충분성 실패 목록."""

    out: list[dict[str, Any]] = []

    p = profile_summary or {}
    if (
        p.get("n_sources", 0) < settings.min_profile_sources
        or p.get("desc_chars", 0) < settings.min_profile_description_chars
        or not p.get("subdomain")
    ):
        out.append(
            _fail(
                company_id,
                "profile",
                "profile_weak",
                f"프로필 근거 부족 (출처 {p.get('n_sources', 0)}건, 설명 {p.get('desc_chars', 0)}자)",
                "웹검색 출처가 있는 사업 설명과 분야",
            )
        )

    fin = summaries.get("financial")
    if fin is not None and (fin.get("coverage") or 0) < settings.min_fin_coverage:
        out.append(
            _fail(
                company_id,
                "financial",
                "fin_coverage_low",
                f"계산 가능한 재무 지표 비율 {fin.get('coverage')} < {settings.min_fin_coverage}",
                "재무제표 항목(공시 PDF 재추출)",
            )
        )

    rag = summaries.get("market_rag")
    if rag is not None:
        if rag.get("status") not in ("rag", "web"):
            out.append(
                _fail(
                    company_id,
                    "market_rag",
                    "market_none",
                    "시장 자료를 찾지 못했습니다",
                    "시장 규모·성장성 근거",
                )
            )
        elif rag.get("n_sources", 0) < settings.min_market_sources:
            out.append(
                _fail(
                    company_id,
                    "market_rag",
                    "market_sources_low",
                    f"시장 출처 {rag.get('n_sources', 0)}건 < {settings.min_market_sources}건",
                    "서로 다른 시장 자료 출처",
                )
            )
        elif not (rag.get("has_size") or rag.get("has_growth")):
            out.append(
                _fail(
                    company_id,
                    "market_rag",
                    "market_no_numbers",
                    "시장 규모·성장률 수치가 없습니다",
                    "시장 규모 또는 성장률 수치",
                )
            )

    score = summaries.get("market_score")
    if score is not None:
        if score.get("n_scored", 0) < max(score.get("n_questions", 10), 1) or score.get(
            "n_without_evidence", 0
        ):
            out.append(
                _fail(
                    company_id,
                    "market_score",
                    "score_incomplete",
                    f"시장성 질문 {score.get('n_scored')}/{score.get('n_questions')}개만 채점, "
                    f"근거 없는 질문 {score.get('n_without_evidence')}개",
                    "근거가 있는 질문별 채점",
                )
            )
        elif (
            score.get("n_domains", 0) < settings.min_score_domains
            or score.get("lowq_share", 1.0) > settings.max_lowquality_share
        ):
            out.append(
                _fail(
                    company_id,
                    "market_score",
                    "score_evidence_weak",
                    f"채점 근거 도메인 {score.get('n_domains')}개, "
                    f"저품질(유튜브·블로그) 비중 {score.get('lowq_share')}",
                    "공신력 있는 다양한 출처",
                )
            )

    comp = summaries.get("competitor")
    if comp is not None:
        if comp.get("found", 0) < settings.min_competitors:
            out.append(
                _fail(
                    company_id,
                    "competitor",
                    "competitor_missing",
                    f"경쟁사 {comp.get('found', 0)}곳 < {settings.min_competitors}곳",
                    "경쟁사 조사 결과",
                )
            )
        elif settings.require_competitor_url and comp.get("grounded", 0) < settings.min_competitors:
            self_ref = comp.get("self_ref", 0)
            out.append(
                _fail(
                    company_id,
                    "competitor",
                    "competitor_self_source" if self_ref else "competitor_ungrounded",
                    f"경쟁사 {comp.get('found')}곳 중 출처가 확인된 타사 {comp.get('grounded', 0)}곳"
                    + (f" (기업 자신의 자료를 경쟁사로 잡은 {self_ref}곳 제외)" if self_ref else "")
                    + f", 방식: {comp.get('method') or '미상'}",
                    "검색으로 확인된 타사 경쟁사와 출처",
                )
            )
    return out


# ═════════════════════════════════════════════════════════════
# 실패 → 재작업 지시
# ═════════════════════════════════════════════════════════════
def directive_for(failure: dict[str, Any], round_no: int) -> dict[str, Any]:
    code = failure.get("code", "")
    base: dict[str, Any] = {"round": round_no, "why": failure.get("reason", "")}
    if code in ("market_sources_low", "market_no_numbers", "market_none"):
        return {**base, "min_sources": 3, "add_web_source": True}
    if code in ("score_incomplete", "score_evidence_weak", "low_source_diversity", "lowquality_share"):
        return {**base, "prefer_institutional": True, "retry_missing_only": True}
    if code in ("competitor_missing", "competitor_ungrounded", "competitor_self_source", "uncited_competitor"):
        return {**base, "provider": "web", "require_source_url": True}
    if code == "neutrality_term":
        return {**base, "neutral_tone": True, "banned": failure.get("needed", "")}
    if code == "fin_coverage_low":
        return {**base, "refetch_filing": True}
    if code in ("worker_error", "missing", "invalid_payload"):
        return {**base, "retry": True}
    return base


# ═════════════════════════════════════════════════════════════
# 보고서 품질 평가 (Groundedness / 중립성 / 편향 통제)
# ═════════════════════════════════════════════════════════════
BANNED_TERMS = (
    "최고",
    "최상",
    "최우수",
    "압도",
    "월등",
    "우월",
    "열등",
    "우위",
    "열위",
    "추천",
    "권장",
    "가장 뛰어",
    "더 낫",
)

LABEL_KIND = {
    "사업 개요": "profile",
    "시장 규모": "market_rag",
    "시장성 점수": "market_score",
    "재무": "financial",
    "경쟁사": "competitor",
}
DERIVED_LABELS = ("판단", "한계·위험")

_CITE = re.compile(r"\[(\d+)\]")
_COMPANY_HEAD = re.compile(r"^###\s+\d+\.\s+(.+?)\s+—\s+")
_CLAIM = re.compile(r"^-\s+\*\*(.+?)\*\*:\s*(.*)$")
_REF = re.compile(r"^\[(\d+)\]\s+(.*)$")


def parse_report(md: str) -> dict[str, Any]:
    """보고서 마크다운 → 기업별 주장(라벨·본문·인용)과 REFERENCE."""

    companies: list[dict[str, Any]] = []
    refs: dict[int, dict[str, str]] = {}
    current: dict[str, Any] | None = None
    section = ""
    for raw in md.split("\n"):
        line = raw.rstrip()
        if line.startswith("## "):
            section = line[3:].strip()
            current = None
            continue
        if section.startswith("REFERENCE"):
            m = _REF.match(line)
            if m:
                body = m.group(2)
                url = ""
                um = re.search(r"(https?://\S+)", body)
                if um:
                    url = um.group(1)
                refs[int(m.group(1))] = {"text": body, "url": url}
            continue
        m = _COMPANY_HEAD.match(line)
        if m:
            current = {"name": m.group(1), "claims": []}
            companies.append(current)
            continue
        c = _CLAIM.match(line)
        if c and current is not None:
            text = c.group(2)
            current["claims"].append(
                {
                    "label": c.group(1),
                    "text": text,
                    "cites": [int(n) for n in _CITE.findall(text)],
                }
            )
    return {"companies": companies, "refs": refs}


def _issue(company, label, kind, code, detail, needed=""):
    return {
        "company": company,
        "label": label,
        "kind": kind,
        "code": code,
        "detail": detail,
        "needed": needed,
    }


def check_report(md: str, settings: Settings) -> dict[str, Any]:
    """3항목 규칙 검사. 각 항목은 {passed, issues[]} 형태입니다."""

    parsed = parse_report(md)
    refs, companies = parsed["refs"], parsed["companies"]

    ground: list[dict] = []
    neutral: list[dict] = []
    bias: list[dict] = []

    cited_all: Counter[int] = Counter()
    for comp in companies:
        for claim in comp["claims"]:
            cited_all.update(claim["cites"])
            kind = LABEL_KIND.get(claim["label"], "report")
            if claim["label"] in LABEL_KIND:
                if not claim["cites"] or "출처 확인 불가" in claim["text"]:
                    ground.append(
                        _issue(
                            comp["name"],
                            claim["label"],
                            kind,
                            "uncited_competitor" if kind == "competitor" else "uncited_claim",
                            "출처 인용이 없는 주장입니다",
                            "해당 주장의 출처",
                        )
                    )
            for n in claim["cites"]:
                if n not in refs:
                    ground.append(
                        _issue(
                            comp["name"],
                            claim["label"],
                            "report",
                            "dangling_citation",
                            f"[{n}] 이 REFERENCE에 없습니다",
                        )
                    )
    for n in refs:
        if n not in cited_all:
            ground.append(
                _issue("", "REFERENCE", "report", "unused_reference", f"[{n}] 이 본문에서 인용되지 않았습니다")
            )

    # 중립성: 우열 판정·추천 표현
    for comp in companies:
        for claim in comp["claims"]:
            for term in BANNED_TERMS:
                if term in claim["text"]:
                    neutral.append(
                        _issue(
                            comp["name"],
                            claim["label"],
                            LABEL_KIND.get(claim["label"], "report"),
                            "neutrality_term",
                            f"우열·추천 표현 '{term}'",
                            term,
                        )
                    )
    summary_part = md.split("## 기업별 평가")[0]
    for term in BANNED_TERMS:
        if term in summary_part:
            neutral.append(_issue("", "SUMMARY", "report", "neutrality_term", f"우열·추천 표현 '{term}'", term))

    # 편향 통제: 출처 다양성, 단일 도메인 편중, 저품질 비중, 반대 근거(한계·위험) 존재
    all_domains: Counter[str] = Counter()
    lowq_cites = total_cites = 0
    for comp in companies:
        domains: set[str] = set()
        for claim in comp["claims"]:
            for n in claim["cites"]:
                ref = refs.get(n)
                if not ref:
                    continue
                d = domain_of(ref["url"])
                domains.add(d)
                all_domains[d] += 1
                total_cites += 1
                if is_lowquality(ref["url"]):
                    lowq_cites += 1
        if len(domains) < settings.min_domains_per_company:
            bias.append(
                _issue(
                    comp["name"],
                    "출처",
                    "market_score",
                    "low_source_diversity",
                    f"인용 출처 도메인 {len(domains)}개 < {settings.min_domains_per_company}개",
                    "서로 다른 도메인의 근거",
                )
            )
        if not any(c["label"] == "한계·위험" and c["text"].strip() for c in comp["claims"]):
            bias.append(
                _issue(comp["name"], "한계·위험", "report", "missing_counter_evidence", "한계·위험 서술이 없습니다")
            )
    if total_cites:
        top_domain, top_n = all_domains.most_common(1)[0]
        if top_n / total_cites > settings.max_top_domain_share:
            bias.append(
                _issue(
                    "",
                    "출처",
                    "market_score",
                    "single_domain_dominance",
                    f"{top_domain} 인용 비중 {top_n}/{total_cites}",
                    "다른 도메인의 근거",
                )
            )
        if lowq_cites / total_cites > settings.max_cited_lowquality_share:
            bias.append(
                _issue(
                    "",
                    "출처",
                    "market_score",
                    "lowquality_share",
                    f"저품질 출처 인용 비중 {lowq_cites}/{total_cites}",
                    "공신력 있는 출처",
                )
            )

    checks = {
        "groundedness": {"passed": not ground, "issues": ground},
        "neutrality": {"passed": not neutral, "issues": neutral},
        "bias_control": {"passed": not bias, "issues": bias},
    }
    return {
        "passed": all(c["passed"] for c in checks.values()),
        "checks": checks,
        "n_companies": len(companies),
        "n_refs": len(refs),
    }


# ═════════════════════════════════════════════════════════════
# 보고서 보정: 품질 평가 피드백으로 report_draft 가 스스로 고칠 수 있는 문제
# ═════════════════════════════════════════════════════════════
REMOVED_NOTE = "(비교 평가성 서술은 제외했습니다)"  # 금지 표현을 포함하면 보정 결과가 다시 중립성 검사에 걸립니다
REPAIRABLE_REPORT_CODES = ("neutrality_term", "unused_reference", "dangling_citation")


def repair_markdown(md: str, feedback: list[dict[str, Any]]) -> str:
    """품질 평가 피드백에 따라 우열·추천 표현이 든 문장을 제거하고 인용·REFERENCE 를 정합하게 맞춥니다."""

    codes = {f.get("code") for f in feedback}
    if "neutrality_term" in codes:
        out_lines = []
        for line in md.split("\n"):
            if line.startswith("[") or line.startswith("#") or not any(t in line for t in BANNED_TERMS):
                out_lines.append(line)
                continue
            # 문장 단위로 금지 표현이 든 문장만 제거 (라벨 접두 '- **라벨**:' 는 유지)
            head, sep, body = line.partition(": ") if line.startswith("- **") else ("", "", line)
            cites = list(dict.fromkeys(_CITE.findall(body)))  # 제거되는 문장에 붙어 있던 인용도 유지
            sentences = re.split(r"(?<=[.다])\s+", body)
            kept = [_CITE.sub("", x).strip() for x in sentences if not any(t in x for t in BANNED_TERMS)]
            body = " ".join(x for x in kept if x).strip() or REMOVED_NOTE
            if cites:
                body += " " + "".join(f"[{c}]" for c in cites)
            else:
                body += " (출처 확인 불가)" if head else ""
            out_lines.append(f"{head}{sep}{body}")
        md = "\n".join(out_lines)
    if codes & {"unused_reference", "dangling_citation"}:
        parsed = parse_report(md)
        body, _, ref_part = md.partition("## REFERENCE")
        cited = sorted({int(n) for n in _CITE.findall(body) if int(n) in parsed["refs"]})
        renumber = {old: new for new, old in enumerate(cited, 1)}
        body = _CITE.sub(lambda m: f"[{renumber[int(m.group(1))]}]" if int(m.group(1)) in renumber else "", body)
        refs_out = []
        for old in cited:
            text = parsed["refs"][old]["text"]
            refs_out.append(f"[{renumber[old]}] {text}")
        md = body + "## REFERENCE\n" + "\n".join(refs_out) + "\n"
    return md
