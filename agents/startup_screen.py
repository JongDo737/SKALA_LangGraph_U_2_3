# 작성자: 신종민
# 파일 설명: RAG로 넘기기 전에 일반 중소·소상공인과 스타트업을 구분하고,
# 웹 검색으로 정확한 투자 라운드(Series)를 확정합니다.
# 자산 기반 단계 추정은 웹에서 라운드를 찾지 못했을 때만 사용합니다.

from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, model_validator

from config import DEFAULT_COMPANY_COUNT, SCREENING_CONCURRENCY, SCREENING_MODEL
from prompts import load_prompt
from state import GraphState

EXACT_STAGES = (
    "Pre-Seed",
    "Seed",
    "Pre-A",
    "Series A",
    "Series B",
    "Series C",
    "Series D",
    "Series E",
    "Series F",
    "IPO",
)
EXACT_STAGE_PATTERN = re.compile(
    r"^(Pre-Seed|Seed|Pre-A|Series [A-F]|IPO)$",
    re.IGNORECASE,
)
STARTUP_HINT_PATTERN = re.compile(r"스타트업으로 분류|스타트업으로 판단|스타트업입니다")
SME_HINT_PATTERN = re.compile(r"일반\s*사업|소상공인|중소기업|스타트업이 아닙")

CLASSIFY_PROMPT = load_prompt("startup_classify")
SERIES_PROMPT = load_prompt("startup_series")


class ClassifyDecision(BaseModel):
    """스타트업 여부만 판단합니다. 라운드는 별도 조사합니다."""

    is_startup: bool
    company_type: Literal["startup", "sme"] = "sme"
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        is_startup = _as_bool(payload.get("is_startup"))
        company_type = _normalize_company_type(payload.get("company_type"), is_startup)
        if company_type == "startup":
            is_startup = True
        payload["is_startup"] = is_startup
        payload["company_type"] = "startup" if is_startup else "sme"
        payload["reason"] = str(payload.get("reason") or "").strip()
        return payload


class SeriesDecision(BaseModel):
    """웹에서 확인한 정확한 투자 라운드입니다."""

    investment_stage: str = "unknown"
    evidence: str = ""
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        stage = _canonicalize_stage(str(payload.get("investment_stage") or ""))
        evidence = str(payload.get("evidence") or "")
        if stage is None:
            stage = _extract_stage_mention(evidence) or "unknown"
        payload["investment_stage"] = stage
        payload["evidence"] = evidence
        sources = payload.get("sources") or []
        if isinstance(sources, str):
            sources = [sources]
        payload["sources"] = [str(item) for item in sources if item]
        return payload


class ScreeningDecision(BaseModel):
    """분류 결과와 라운드 조사 결과를 합친 최종 스크리닝입니다."""

    is_startup: bool
    company_type: Literal["startup", "sme"]
    investment_stage: str
    stage_source: Literal["web_search", "asset_fallback", "unknown"]
    reason: str = ""
    sources: list[str] = Field(default_factory=list)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "startup"}


def _normalize_company_type(value: Any, is_startup: bool) -> Literal["startup", "sme"]:
    text = str(value or "").strip().lower()
    if text in {"startup", "start-up", "start_up", "스타트업"}:
        return "startup"
    if text in {"sme", "smb", "general", "일반", "중소", "소상공인"}:
        return "sme"
    return "startup" if is_startup else "sme"


def _company_name(company: dict[str, Any]) -> str:
    return str(company.get("company_name") or company.get("name") or "이름 없음")


def _asset_estimated_stage(company: dict[str, Any]) -> str:
    return str(
        company.get("asset_estimated_stage")
        or (company.get("dart") or {}).get("asset_estimated_stage")
        or company.get("estimated_investment_stage")
        or (company.get("dart") or {}).get("estimated_investment_stage")
        or ""
    )


def _canonicalize_stage(stage: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", stage).strip()
    cleaned = cleaned.replace("시리즈", "Series").replace("시드", "Seed")
    cleaned = cleaned.replace("프리에이", "Pre-A").replace("프리 A", "Pre-A")
    cleaned = cleaned.replace("Pre Seed", "Pre-Seed").replace("Pre A", "Pre-A")
    if "~" in cleaned or "추정" in cleaned or cleaned.startswith("자산추정"):
        return None
    match = EXACT_STAGE_PATTERN.match(cleaned)
    if not match:
        return None
    found = match.group(0)
    for exact in EXACT_STAGES:
        if found.lower() == exact.lower():
            return exact
    return found


def _extract_stage_mention(text: str) -> str | None:
    normalized = (text or "").replace("시리즈", "Series ").replace("시드", "Seed ")
    match = re.search(
        r"(Pre[-\s]?Seed|Seed|Pre[-\s]?A|Series\s*[A-F]|IPO)",
        normalized,
        re.IGNORECASE,
    )
    if not match:
        return None
    return _canonicalize_stage(match.group(1))


def has_exact_investment_stage(stage: str | None) -> bool:
    """웹에서 확인된 단일 라운드만 정확한 단계로 봅니다."""

    return bool(stage and _canonicalize_stage(stage))


def _is_already_screened(company: dict[str, Any]) -> bool:
    screening = company.get("screening") or {}
    return screening.get("is_startup") is True


def _build_llm(*, enable_web_search: bool) -> ChatOpenAI:
    model = ChatOpenAI(
        model=SCREENING_MODEL,
        temperature=0,
        use_responses_api=True,
        output_version="responses/v1",
    )
    if enable_web_search:
        return model.bind_tools([{"type": "web_search"}])
    return model


def _snippet(text: str, limit: int = 220) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "..."


def _format_error(error: BaseException, raw_text: str = "") -> str:
    """스크리닝 실패 원인을 로그에 바로 보이게 만듭니다."""

    if isinstance(error, RuntimeError) and str(error).strip():
        return str(error).strip()

    error_type = type(error).__name__
    message = str(error).strip() or "메시지 없음"
    if isinstance(error, json.JSONDecodeError):
        snippet = _snippet(raw_text or error.doc or "")
        return (
            f"{error_type}: 모델이 JSON이 아닌 응답을 반환했습니다. "
            f"{error.msg} (위치 {error.pos}). 응답 일부: {snippet or '없음'}"
        )
    if "validation" in error_type.lower() or error_type == "ValidationError":
        return f"{error_type}: 모델 JSON 필드가 스키마와 맞지 않습니다. {message}"
    if error.__cause__:
        cause = error.__cause__
        return (
            f"{error_type}: {message} "
            f"| 원인 {type(cause).__name__}: {str(cause).strip() or '메시지 없음'}"
        )
    return f"{error_type}: {message}"


def _block_text(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        item_type = str(item.get("type") or "")
        if item_type in {"web_search_call", "tool_call", "function_call"}:
            return ""
        for key in ("text", "output_text", "content"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if value not in {None, ""}:
                nested = _extract_text(value)
                if nested:
                    return nested
        return ""
    text = getattr(item, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    content = getattr(item, "content", None)
    if content not in {None, item}:
        return _extract_text(content)
    return ""


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part for part in (_block_text(item) for item in content) if part)
    if isinstance(content, dict):
        return _block_text(content)
    text_method = getattr(content, "text", None)
    if callable(text_method):
        try:
            extracted = text_method()
            if isinstance(extracted, str) and extracted.strip():
                return extracted
        except Exception:
            pass
    nested = getattr(content, "content", None)
    if nested not in {None, content}:
        return _extract_text(nested)
    return str(content)


def _extract_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        raise json.JSONDecodeError("JSON 객체를 찾지 못했습니다", text, 0)

    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[start:], start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise json.JSONDecodeError("닫히지 않은 JSON 객체입니다", text, start)


def _parse_model(raw: Any, model_cls: type[BaseModel]) -> Any:
    text = _extract_text(raw).strip()
    if not text:
        raise json.JSONDecodeError(
            f"모델 응답 텍스트가 비어 있습니다. content 타입={type(raw).__name__}",
            _snippet(repr(raw)),
            0,
        )
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = json.loads(_extract_json_object(text))
    if not isinstance(payload, dict):
        raise json.JSONDecodeError("JSON 객체가 아닙니다", text, 0)
    return model_cls.model_validate(payload)


def _company_context(company: dict[str, Any]) -> str:
    return (
        f"기업명: {_company_name(company)}\n"
        f"대표: {company.get('ceo')}\n"
        f"설립일: {company.get('established_date') or company.get('established_at')}\n"
        f"법인구분: {company.get('legal_class')}\n"
        f"DART 자산추정 단계(참고용, 확정 아님): {_asset_estimated_stage(company) or '없음'}\n"
        f"재무요약: {json.dumps(company.get('financial_summary') or {}, ensure_ascii=False)}\n"
        f"공시링크: {company.get('dart_viewer_link')}\n"
    )


def _reconcile_classification(decision: ClassifyDecision) -> ClassifyDecision:
    reason = decision.reason
    if not decision.is_startup and STARTUP_HINT_PATTERN.search(reason):
        if not SME_HINT_PATTERN.search(reason):
            decision.is_startup = True
            decision.company_type = "startup"
    return decision


async def _invoke_json(
    *,
    prompt: str,
    human: str,
    model_cls: type[BaseModel],
    enable_web_search: bool,
    semaphore: asyncio.Semaphore,
) -> Any:
    llm = _build_llm(enable_web_search=enable_web_search)
    async with semaphore:
        result = await llm.ainvoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=human),
            ]
        )
    raw_content = getattr(result, "content", result)
    try:
        return _parse_model(raw_content, model_cls)
    except Exception as error:
        raise RuntimeError(_format_error(error, _extract_text(raw_content))) from error


async def _classify_company(
    company: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> ClassifyDecision:
    human = (
        "스타트업 여부만 판단하세요. 투자 라운드는 비워도 됩니다.\n"
        "반드시 JSON만 반환하세요.\n\n"
        f"{_company_context(company)}"
    )
    decision = await _invoke_json(
        prompt=CLASSIFY_PROMPT,
        human=human,
        model_cls=ClassifyDecision,
        enable_web_search=False,
        semaphore=semaphore,
    )
    return _reconcile_classification(decision)


async def _lookup_exact_series(
    company: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> SeriesDecision:
    name = _company_name(company)
    human = (
        f"다음 검색으로 정확한 단일 라운드만 찾으세요.\n"
        f'- "{name} 투자 유치"\n'
        f'- "{name} 시리즈 투자"\n'
        f'- "{name} Series"\n'
        f'- "{name} 더벨"\n\n'
        "범위나 추정은 unknown으로 두세요. 반드시 JSON만 반환하세요.\n\n"
        f"{_company_context(company)}"
    )
    decision = await _invoke_json(
        prompt=SERIES_PROMPT,
        human=human,
        model_cls=SeriesDecision,
        enable_web_search=True,
        semaphore=semaphore,
    )
    mentioned = _extract_stage_mention(decision.evidence)
    if decision.investment_stage == "unknown" and mentioned:
        decision.investment_stage = mentioned
    return decision


async def _screen_one_company(
    company: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> ScreeningDecision:
    classified = await _classify_company(company, semaphore)
    if not classified.is_startup:
        return ScreeningDecision(
            is_startup=False,
            company_type="sme",
            investment_stage="unknown",
            stage_source="unknown",
            reason=classified.reason,
            sources=[],
        )

    series = await _lookup_exact_series(company, semaphore)
    exact = _canonicalize_stage(series.investment_stage)
    if exact:
        reason = classified.reason
        if series.evidence:
            reason = f"{classified.reason} / 라운드: {series.evidence}".strip(" /")
        return ScreeningDecision(
            is_startup=True,
            company_type="startup",
            investment_stage=exact,
            stage_source="web_search",
            reason=reason,
            sources=series.sources,
        )

    fallback = _asset_estimated_stage(company) or "자산추정 확인 불가"
    return ScreeningDecision(
        is_startup=True,
        company_type="startup",
        investment_stage=fallback,
        stage_source="asset_fallback",
        reason=(
            f"{classified.reason} / 웹에서 정확한 Series를 찾지 못해 "
            f"자산 추정을 마지막 fallback으로 사용했습니다."
        ).strip(" /"),
        sources=series.sources,
    )


async def _screen_candidate(
    company: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> tuple[dict[str, Any], ScreeningDecision | None, str | None]:
    """한 기업 스크리닝 결과와 실패 사유를 함께 반환합니다."""

    if _is_already_screened(company):
        return company, None, None
    try:
        decision = await _screen_one_company(company, semaphore)
        return company, decision, None
    except Exception as error:
        return company, None, _format_error(error)


def _source_label(source: str) -> str:
    if source == "web_search":
        return "웹검색"
    if source == "asset_fallback":
        return "자산fallback"
    return "미확인"


async def screen_startups_node(state: GraphState) -> dict[str, Any]:
    """DART 결과를 스타트업만 남기고, 목표 개수만큼 RAG로 넘깁니다."""

    target_count = int(state.get("target_company_count", DEFAULT_COMPANY_COUNT))
    candidates = list(state.get("eligible_companies", []))
    passed: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = list(state.get("startup_rejections", []))
    semaphore = asyncio.Semaphore(SCREENING_CONCURRENCY)

    print("\n[노드 실행] screen_startups (agents/startup_screen.py)")
    print(f"  입력 State : 후보={[_company_name(item) for item in candidates]}")
    print(
        f"  처리 방식 : {len(candidates)}개 기업 병렬 스크리닝 "
        f"(동시 {SCREENING_CONCURRENCY}개, Series는 웹검색 우선)"
    )

    results: list[Any] = []
    if candidates:
        results = await asyncio.gather(
            *[_screen_candidate(company, semaphore) for company in candidates],
            return_exceptions=True,
        )

    for result in results:
        if isinstance(result, Exception):
            reason = _format_error(result)
            rejected.append({"name": "알 수 없음", "reason": reason})
            print(f"  제외: 알 수 없음 / {reason}")
            continue

        company, decision, error_reason = result
        name = _company_name(company)
        if error_reason:
            rejected.append({"name": name, "reason": error_reason})
            print(f"  제외: {name} / {error_reason}")
            continue

        if decision is None:
            passed.append(company)
            print(f"  유지: {name} / 이전 스크리닝 통과")
            continue

        if not decision.is_startup:
            rejected.append({"name": name, "reason": decision.reason})
            print(f"  제외: {name} / 일반사업 / {decision.reason}")
            continue

        enriched = deepcopy(company)
        enriched["estimated_investment_stage"] = decision.investment_stage
        enriched["stage_source"] = decision.stage_source
        if not enriched.get("asset_estimated_stage"):
            enriched["asset_estimated_stage"] = _asset_estimated_stage(company)
        enriched["screening"] = decision.model_dump()
        passed.append(enriched)
        source_note = ""
        if decision.stage_source == "web_search" and decision.sources:
            source_note = f" / {decision.sources[0]}"
        print(
            f"  통과: {name} / {decision.investment_stage} "
            f"/ 출처={_source_label(decision.stage_source)}{source_note}"
        )

    passed = passed[:target_count]
    message = (
        f"스타트업 검증: 통과 {len(passed)}/{target_count}개, "
        f"제외 {len(rejected)}개"
    )
    print(
        f"  반환 값    : 통과={[ _company_name(item) for item in passed ]}, "
        f"{len(passed)}/{target_count}개"
    )

    return {
        "eligible_companies": passed,
        "startup_rejections": rejected,
        "next_stage": "market_research",
        "execution_log": [*state.get("execution_log", []), message],
    }
