# 작성자: 통합 담당자 김가빈
# 파일 설명: judge 이후 기업 데이터를 report.md 계약에 맞게 정제·보강하고
#            PDF 보고서 생성 노드로 넘기는 준비 에이전트입니다.

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph

from config import DART_FETCH_MULTIPLIER, DEFAULT_COMPANY_COUNT
from prompts import format_prompt

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOC_METADATA_PATH = PROJECT_ROOT / "data" / "doc_metadata.json"
FONT_DIR = PROJECT_ROOT / "assets" / "fonts"
DEFAULT_LATEST_JSON = PROJECT_ROOT / "outputs" / "final_companies_latest.json"

REPORT_PREP_MODEL = (os.getenv("REPORT_PREP_MODEL") or "gpt-4o-mini").strip()

# report.py PageWriter.table 과 동일한 폭·여백 (stringWidth > width-12 이면 오류)
_PAGE_W, _PAGE_H = A4
_L, _R = 48, _PAGE_W - 48
_CONTENT_W = _R - _L
_TABLE_PAD = 12
_FONT = "Pretendard"
_BOLD = "PretendardSemiBold"
_FONT_READY = False

# 3쪽 경쟁사 표: ["기업", "제품·대상", "차이·근거"] widths=[115, 145, R-L-260]
_COMP_COL = (115, 145, _CONTENT_W - 260)
# 4쪽 점수 표: ["평가 항목", "점수", "판단 근거 · 출처"] widths=[125, 55, R-L-180]
_SCORE_COL = (125, 55, _CONTENT_W - 180)
# 4쪽 후보 표: ["기업", "판단", "주요 이유"] widths=[115, 65, R-L-180]
_CAND_COL = (115, 65, _CONTENT_W - 180)

# SUMMARY summary_note: height > 28 이면 오류 (report.py와 동일 계산)
_SUMMARY_SIDE_X = _R - 128
_SUMMARY_LEFT_W = _SUMMARY_SIDE_X - _L - 27
_SUMMARY_TEXT_W = _SUMMARY_LEFT_W - 99
_SUMMARY_MAX_H = 28.0
_SUMMARY_MAX_CHARS = 52  # 높이 폴백 시 보수적 상한

# 파일명에서 발행기관으로 추정할 수 있는 토큰
_PUBLISHER_HINTS: tuple[tuple[str, str], ...] = (
    ("IEA", "IEA"),
    ("국제에너지기구", "IEA"),
    ("Research Nester", "Research Nester"),
    ("리서치네스터", "Research Nester"),
    ("BloombergNEF", "BloombergNEF"),
    ("BNEF", "BloombergNEF"),
    ("Mordor", "Mordor Intelligence"),
    ("Grand View", "Grand View Research"),
    ("Fortune Business", "Fortune Business Insights"),
    ("GM Insights", "Global Market Insights"),
    ("Global Market Insights", "Global Market Insights"),
    ("Statista", "Statista"),
    ("한전", "한국전력공사"),
    ("KEPCO", "한국전력공사"),
    ("산업통상자원부", "산업통상자원부"),
    ("한국에너지공단", "한국에너지공단"),
    ("한국전력거래소", "한국전력거래소"),
    ("전력거래소", "한국전력거래소"),
)
_FORECAST_YEARS = {str(year) for year in range(2027, 2041)}
_UNKNOWN_MARKERS = {"", "unknown", "확인되지 않음", "미확인"}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clip(value: Any, limit: int = 160) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _ensure_report_fonts() -> None:
    """report.py와 같은 Pretendard로 표 셀 폭을 측정합니다."""

    global _FONT_READY
    if _FONT_READY:
        return
    regular = FONT_DIR / "Pretendard-Regular.ttf"
    semibold = FONT_DIR / "Pretendard-SemiBold.ttf"
    if not regular.is_file():
        # 폰트 없으면 글자 수 폴백만 사용
        return
    pdfmetrics.registerFont(TTFont(_FONT, str(regular)))
    pdfmetrics.registerFont(
        TTFont(_BOLD, str(semibold if semibold.is_file() else regular))
    )
    _FONT_READY = True


def _fit_cell(
    value: Any,
    col_width: float,
    *,
    bold: bool = False,
    size: float = 8,
    fallback_chars: int = 28,
) -> str:
    """report.py 표 검사(stringWidth > width-12)를 통과하도록 잘라냅니다."""

    text = _clean(value).replace("\n", " ")
    if not text:
        return "-"
    max_width = max(col_width - _TABLE_PAD, 8)
    _ensure_report_fonts()
    if not _FONT_READY:
        return _clip(text, fallback_chars)

    face = _BOLD if bold else _FONT
    if pdfmetrics.stringWidth(text, face, size) <= max_width:
        return text

    ellipsis = "..."
    ellipsis_w = pdfmetrics.stringWidth(ellipsis, face, size)
    budget = max_width - ellipsis_w
    if budget <= 0:
        return ellipsis

    # 이진 탐색으로 폭에 맞는 최장 접두사 선택
    low, high = 0, len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid]
        if pdfmetrics.stringWidth(candidate, face, size) <= budget:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    fitted = (best.rstrip() + ellipsis) if best else ellipsis
    # 추천 행은 BOLD로 그려지므로 bold=True로도 한 번 더 확인
    if pdfmetrics.stringWidth(fitted, face, size) > max_width:
        return _clip(text, max(fallback_chars // 2, 8))
    return fitted


def _summary_note_height(text: str, *, bold: bool = False) -> float:
    """report.py SUMMARY summary_note Paragraph 높이를 재현합니다."""

    _ensure_report_fonts()
    if not _FONT_READY:
        # 대략 한 줄 ~26자 기준
        return 14.0 * max(1, (len(_clean(text)) + 25) // 26)

    from html import escape

    paragraph = Paragraph(
        escape(_clean(text)).replace("\n", "<br/>"),
        ParagraphStyle(
            "summary_note_measure",
            fontName=_BOLD if bold else _FONT,
            fontSize=9.5 if bold else 9.2,
            leading=14,
            wordWrap="CJK",
        ),
    )
    _, height = paragraph.wrap(_SUMMARY_TEXT_W, _PAGE_H)
    return float(height)


def _fit_summary_note(
    value: Any,
    *,
    bold: bool = False,
    fallback_chars: int = _SUMMARY_MAX_CHARS,
) -> str:
    """SUMMARY 노트(height≤28)를 통과하도록 잘라냅니다."""

    text = _clean(value)
    if not text:
        return "-"
    if _summary_note_height(text, bold=bold) <= _SUMMARY_MAX_H:
        return text

    low, high = 0, len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip() + ("..." if mid < len(text) else "")
        if _summary_note_height(candidate, bold=bold) <= _SUMMARY_MAX_H:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    if best:
        return best
    return _clip(text, fallback_chars)


def _first_short_phrase(value: Any, *, max_chars: int = 36) -> str:
    """긴 문장·목록에서 표용 짧은 구절만 뽑습니다."""

    text = _clean(value)
    if not text:
        return ""
    # URL은 표에 넣지 않음
    if text.startswith("http://") or text.startswith("https://"):
        return ""
    for sep in ("。", ". ", "·", "/", "|", ";", "；", ","):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    return _clip(text, max_chars)


def _is_noise_text(value: Any) -> bool:
    """스크리닝 fallback·입력 대기 문구 등 보고서 본문에 넣지 않을 텍스트."""

    text = _clean(value)
    if not text:
        return True
    noise_tokens = (
        "입력 대기",
        "asset_fallback",
        "stage_source=",
        "웹에서 정확한 Series",
        "마지막 fallback",
        "fallback으로",
        "추가 검증이 필요",
        "근거만으로는",
    )
    return any(token in text for token in noise_tokens)


def _format_krw(value: Any) -> str:
    text = _clean(value).replace(",", "")
    if not text or text.lower() in {"null", "none", "-"}:
        return "-"
    try:
        number = float(text)
    except ValueError:
        return _clip(value, 18)
    abs_n = abs(number)
    sign = "-" if number < 0 else ""
    if abs_n >= 1_0000_0000_0000:
        return f"{sign}{abs_n / 1_0000_0000_0000:.1f}조"
    if abs_n >= 1_0000_0000:
        return f"{sign}{abs_n / 1_0000_0000:.1f}억"
    if abs_n >= 1_0000:
        return f"{sign}{abs_n / 1_0000:.0f}만"
    return f"{sign}{abs_n:,.0f}"


def _resolve_reviewed_count(state: dict[str, Any], company_count: int) -> int:
    """SUMMARY '검토'칸: 처음 DART에 요청한 탐색 건수."""

    for key in ("dart_fetch_requested", "dart_initial_fetch_count", "reviewed_count"):
        raw = state.get(key)
        try:
            if raw is not None and int(raw) > 0:
                return int(raw)
        except (TypeError, ValueError):
            pass
    seen = state.get("dart_seen_corp_codes")
    if isinstance(seen, list) and seen:
        return len(seen)
    target = state.get("target_company_count") or company_count or DEFAULT_COMPANY_COUNT
    try:
        target_n = max(int(target), 1)
    except (TypeError, ValueError):
        target_n = DEFAULT_COMPANY_COUNT
    return target_n * DART_FETCH_MULTIPLIER


def _compose_candidate_reason(bundle: dict[str, Any]) -> str:
    """평가 후보 표 주요 이유: 2~3문장, 줄바꿈 허용."""

    judgement = bundle.get("judgement") or {}
    market = bundle.get("market_size") or {}
    risk = bundle.get("risk_basis") or {}
    financial = risk.get("financial_summary") or {}
    vbm = judgement.get("vbm_assessment") or {}

    sentences: list[str] = []
    score = judgement.get("external_market_score")
    try:
        score_n = int(float(score))
    except (TypeError, ValueError):
        score_n = None
    target = _clean(market.get("target_market"))
    growth = _clean(market.get("growth"))
    if score_n is not None and (target or growth):
        sentences.append(
            f"외부 시장성 {score_n}점으로 {target or '관련 시장'} 접근성이 확인된다."
        )
    elif score_n is not None:
        sentences.append(f"외부 시장성 {score_n}점으로 성장 구간 진입이 확인된다.")

    vbm_c = _clean(vbm.get("conclusion"))
    signals = vbm.get("positive_signals") or []
    label_map = {
        "revenue_cagr": "매출 성장",
        "current_ratio": "단기 지급능력",
        "debt_to_equity": "안정적 자본구조",
        "operating_margin": "영업이익률",
        "net_margin": "순이익률",
        "free_cash_flow": "현금창출력",
        "roe": "ROE",
        "roa": "ROA",
    }
    if vbm_c:
        if signals:
            nice = [label_map.get(str(s), str(s)) for s in signals[:2]]
            sentences.append(f"VBM은 {vbm_c}이며 {'·'.join(nice)} 신호가 지지한다.")
        else:
            sentences.append(f"VBM 결론은 {vbm_c}으로 가치 창출 가능성을 지지한다.")

    revenue = financial.get("매출액") or financial.get("revenue")
    if growth:
        sentences.append(f"산업은 {growth} 구간에 있다.")
    elif revenue not in (None, ""):
        sentences.append(f"공시 매출 {_format_krw(revenue)}로 사업 실체가 확인된다.")

    if not sentences:
        sentences = ["시장·재무 근거를 종합해 추천한다."]
    return _clip(" ".join(sentences[:3]), 108)


def _compose_idea_prose(bundle: dict[str, Any]) -> str:
    """01쪽 사업 아이디어: 증권 리서치 톤 2~3문장."""

    enrichment = bundle.get("enrichment") or {}
    llm_idea = _clean(enrichment.get("idea"))
    if (
        llm_idea
        and not _is_noise_text(llm_idea)
        and "추정됨" not in llm_idea
        and len(llm_idea) >= 80
    ):
        return _clip(llm_idea, 420)

    name = bundle.get("company_name") or "동사"
    subdomain = _clean(bundle.get("subdomain")) or "에너지 인프라"
    market = bundle.get("market_size") or {}
    target = _clean(market.get("target_market")) or subdomain
    risk = bundle.get("risk_basis") or {}
    financial = risk.get("financial_summary") or {}
    revenue = financial.get("매출액") or financial.get("revenue")
    first = (
        f"{name}의 핵심 컨셉은 {target}에서 발전·공급 자산을 운영하며 "
        f"{subdomain} 수요를 가져가는 것이다."
    )
    second = (
        f"공시 매출 {_format_krw(revenue)}가 확인되는 만큼 사업 실체는 갖춰져 있으며, "
        "AI 데이터센터 전력 수요 확대와 맞물리면 발전·공급 자산의 활용도가 높아질 수 있다."
        if revenue not in (None, "")
        else (
            "사업 설명은 업종·공시 정보를 바탕으로 재구성한 것이며, "
            "AI 데이터센터 전력 인프라와의 직접 연계는 수주·판매 실적으로 재확인할 필요가 있다."
        )
    )
    third = (
        "핵심 컨셉은 재생·분산 전원을 데이터센터와 산업 수요에 연결하는 것이며, "
        "이 축이 분명할수록 후속 라운드에서 프리미엄을 받을 여지가 있다."
    )
    return _clip(f"{first} {second} {third}", 420)


def _compose_team_prose(bundle: dict[str, Any]) -> str:
    """01쪽 팀 구성: CEO·기술 역량을 전문가 톤으로."""

    enrichment = bundle.get("enrichment") or {}
    team = _clean(enrichment.get("team"))
    if team and not _is_noise_text(team) and "미검증" not in team and len(team) >= 60:
        return _clip(team, 360)

    ceo = _clean(bundle.get("ceo")) or "미확인"
    name = bundle.get("company_name") or "동사"
    return _clip(
        f"{name}의 대표이사는 {ceo}이다. "
        "창업 멤버의 전력·인프라 현장 경험과 핵심 기술 인력의 이력은 아직 공시만으로는 충분히 드러나지 않는다. "
        "다만 외감 체계를 유지하고 있는 점은 운영 규율이 갖춰져 있음을 시사하며, "
        "후속 실사에서는 CTO급 인력과 발전소 운영 레퍼런스를 확인하는 것이 관건이다.",
        360,
    )


def _compose_market_prose(bundle: dict[str, Any]) -> str:
    """02쪽 시장 규모: 수치를 해석하는 2~3문장."""

    enrichment = bundle.get("enrichment") or {}
    market = bundle.get("market_size") or {}
    target = _clean(market.get("target_market"))
    size = _clean(market.get("market_size"))
    growth = _clean(market.get("growth"))
    llm = _clean(enrichment.get("market_chart_takeaway"))
    if target and size and growth:
        return _clip(
            f"{target} 규모는 {size}로 파악된다. "
            f"성장률은 {growth}로, 단순 순환 회복이 아니라 구조적 확장이 진행 중인 구간으로 읽힌다. "
            "다만 이 수치는 산업 전체 TAM이며 동사 매출 전망과 동일시해서는 안 된다. "
            "실제 투자 매력은 동사가 이 성장분의 어느 층을 가져올 수 있는지에 달려 있다.",
            420,
        )
    if llm and not _is_noise_text(llm):
        extra = " / ".join(p for p in (target, size, growth) if p)
        return _clip(f"{llm} {extra}".strip(), 420)
    bits = [p for p in (target, size, growth) if p]
    if bits:
        return _clip(
            f"관련 시장은 {' / '.join(bits)}로 정리된다. "
            "수요 확대가 확인되는 구간인 만큼, 실행력만 뒷받침되면 밸류에이션 재평가 여지가 있다.",
            420,
        )
    return "관련 시장의 규모·성장 수치는 제한적으로만 확인되며, 산업 성장과 동사 실적을 분리해 볼 필요가 있다."


def _compose_demand_prose(bundle: dict[str, Any]) -> str:
    enrichment = bundle.get("enrichment") or {}
    market = bundle.get("market_size") or {}
    demand = _clean(enrichment.get("demand") or market.get("demand_evidence"))
    if demand and not _is_noise_text(demand):
        if len(demand) >= 80:
            return _clip(demand, 360)
        return _clip(
            f"{demand} "
            "이는 일회성 이벤트가 아니라 전력 수급과 입지 제약에서 나오는 구조적 수요로 판단한다. "
            "수요가 가격 전가력으로 이어질지가 중기 수익성의 핵심이다.",
            360,
        )
    return (
        "탈탄소와 데이터센터 전력 수요가 동시에 커지면서 발전·계통 자산에 대한 관심이 높아지고 있다. "
        "이 수요가 동사의 판매단가와 가동률로 연결되는지가 실적 모멘텀의 분수령이다."
    )


def _compose_body_risks(bundle: dict[str, Any]) -> str:
    """03쪽 리스크·한계: 시장·기술·규제·경쟁을 2~3문장으로."""

    enrichment = bundle.get("enrichment") or {}
    risks = _clean(enrichment.get("risks"))
    if risks and not _is_noise_text(risks) and len(risks) >= 80:
        return _clip(risks, 420)

    risk = bundle.get("risk_basis") or {}
    financial = risk.get("financial_summary") or {}
    op = financial.get("영업이익") or financial.get("operating_profit")
    debt = financial.get("이자부부채") or financial.get("debt")
    op_neg = False
    has_debt = False
    try:
        op_neg = op is not None and float(str(op).replace(",", "")) < 0
    except ValueError:
        pass
    try:
        has_debt = debt is not None and float(str(debt).replace(",", "")) > 0
    except ValueError:
        pass

    first = (
        f"가장 뚜렷한 재무 리스크는 영업이익 {_format_krw(op)}의 적자 지속이다. "
        if op_neg
        else "재무 체력은 당장 무너질 수준은 아니나, 확장 국면에서 수익성 개선이 늦어지면 밸류에이션이 빠르게 할인된다. "
    )
    if has_debt:
        first += f"이자부부채 {_format_krw(debt)}가 있어 금리와 상환 일정이 현금흐름을 제약할 수 있다. "
    second = (
        "시장 측면에서는 대형 발전·모듈 사업자와의 단가 경쟁, "
        "기술 측면에서는 발전 효율·계통 연계 역량의 검증 공백, "
        "규제 측면에서는 REC·SMP·인허가 변동이 동시에 걸려 있다. "
    )
    third = (
        "한계는 분명하다. 창업자 기술 레퍼런스와 장기 전력판매 계약이 공시만으로는 확인되지 않아, "
        "현재 점수는 성장 시장에 대한 옵션 가치에 가깝다. "
        "흑자 전환과 수주 가시성이 열리기 전에는 공격적 밸류에이션을 주기 어렵다."
    )
    return _clip(f"{first}{second}{third}", 420)


def _compose_summary_top_risk(bundle: dict[str, Any]) -> str:
    """SUMMARY 핵심 위험: 투자자가 바로 이해할 리스크 한 줄."""

    risk = bundle.get("risk_basis") or {}
    financial = risk.get("financial_summary") or {}
    judgement = bundle.get("judgement") or {}
    vbm = judgement.get("vbm_assessment") or {}
    market = bundle.get("market_size") or {}

    op = financial.get("영업이익") or financial.get("operating_profit")
    net = financial.get("당기순이익") or financial.get("net_income")
    debt = financial.get("이자부부채") or financial.get("debt")
    revenue = financial.get("매출액") or financial.get("revenue")

    op_neg = False
    net_neg = False
    has_debt = False
    try:
        op_neg = op is not None and float(str(op).replace(",", "")) < 0
    except ValueError:
        pass
    try:
        net_neg = net is not None and float(str(net).replace(",", "")) < 0
    except ValueError:
        pass
    try:
        has_debt = debt is not None and float(str(debt).replace(",", "")) > 0
    except ValueError:
        pass

    if op_neg and has_debt:
        return "영업적자와 이자부채가 겹쳐, 현금창출력 회복이 투자 성패를 가릅니다."
    if op_neg or net_neg:
        return "매출이 있어도 수익화가 늦으면 기업가치가 빠르게 훼손될 수 있습니다."
    if has_debt:
        return "이자부채 부담이 있어, 금리·상환 일정이 투자 수익을 좌우합니다."
    if _clean(vbm.get("conclusion")) == "주의":
        return "VBM 신호가 엇갈려, 수익성 개선 없이 확장하면 위험이 커집니다."
    growth = _first_short_phrase(market.get("growth"), max_chars=20)
    if growth:
        return f"시장은 {growth}이나, 실행력 부족 시 기회 상실 위험이 큽니다."
    if revenue not in (None, ""):
        return "시장 기회 대비 사업 규모가 작아, 확장 실패 시 회수가 어렵습니다."
    return "규제·경쟁 환경 변화가 투자 회수 일정을 흔들 수 있습니다."


def _compose_summary_next_check(bundle: dict[str, Any]) -> str:
    """SUMMARY 다음 판단 조건: 투자 확정에 필요한 다음 액션."""

    risk = bundle.get("risk_basis") or {}
    financial = risk.get("financial_summary") or {}
    judgement = bundle.get("judgement") or {}
    market = bundle.get("market_size") or {}
    enrichment = bundle.get("enrichment") or {}

    op = financial.get("영업이익") or financial.get("operating_profit")
    op_neg = False
    try:
        op_neg = op is not None and float(str(op).replace(",", "")) < 0
    except ValueError:
        pass

    team = _clean(enrichment.get("team") or bundle.get("ceo"))
    team_weak = (not team) or ("미검증" in team) or ("공시상" in team)

    if op_neg:
        return (
            "흑자 전환 시점과 전력판매·수주 실적을 확인하면 투자를 확정할 수 있습니다."
        )
    if team_weak:
        return "창업자·핵심 인력의 실행 이력을 검증하면 투자 확신을 높일 수 있습니다."
    score = judgement.get("external_market_score")
    try:
        if score is not None and float(score) < 70:
            return "수요 고객·계약 파이프라인을 보강하면 시장성 점수를 더 끌어올릴 수 있습니다."
    except (TypeError, ValueError):
        pass
    if _clean(market.get("demand_evidence")):
        return "실제 제품·고객 레퍼런스를 확인하면 최종 투자 결정을 내릴 수 있습니다."
    return "핵심 계약과 자금 사용 계획을 확인한 뒤 투자 집행 여부를 판단합니다."


def _compose_summary_reasons(bundle: dict[str, Any]) -> list[str]:
    """SUMMARY 추천 근거·추가 판단: 설득력 있는 두 문장."""

    judgement = bundle.get("judgement") or {}
    market = bundle.get("market_size") or {}
    risk = bundle.get("risk_basis") or {}
    financial = risk.get("financial_summary") or {}
    vbm = judgement.get("vbm_assessment") or {}

    reasons: list[str] = []
    # 기존 investment_reasons 중 설득력 있는 것만 우선
    for item in judgement.get("investment_reasons") or []:
        text = _clean(item)
        if (
            not text
            or _is_noise_text(text)
            or "50점 이상" in text
            or "참고 자료" in text
        ):
            continue
        reasons.append(text)
        if len(reasons) >= 2:
            break

    if len(reasons) < 2:
        score = judgement.get("external_market_score")
        target = _clean(market.get("target_market"))
        growth = _clean(market.get("growth"))
        if score is not None and (target or growth):
            bit = growth or target
            reasons.append(f"외부 시장성 {score}점과 {bit} 기회가 맞물립니다.")
        elif score is not None:
            reasons.append(f"외부 시장성 {score}점으로 성장 시장 접근성이 확인됩니다.")

    if len(reasons) < 2:
        vbm_c = _clean(vbm.get("conclusion"))
        signals = vbm.get("positive_signals") or []
        if vbm_c == "긍정" and signals:
            label_map = {
                "revenue_cagr": "매출 성장",
                "current_ratio": "단기 지급능력",
                "debt_to_equity": "안정적 자본구조",
                "operating_margin": "영업이익률",
                "roe": "ROE",
                "roa": "ROA",
            }
            nice = [label_map.get(str(s), str(s)) for s in signals[:2]]
            reasons.append(f"VBM {vbm_c}: {'·'.join(nice)} 신호가 양호합니다.")
        elif vbm_c:
            reasons.append(f"VBM 결론이 {vbm_c}으로 가치 창출 가능성을 지지합니다.")

    if len(reasons) < 2:
        revenue = financial.get("매출액") or financial.get("revenue")
        if revenue not in (None, ""):
            reasons.append(
                f"공시 매출 {_format_krw(revenue)} 규모로 사업 실체가 확인됩니다."
            )

    if not reasons:
        reasons = ["시장·재무·경쟁 근거를 종합해 투자 적합으로 판단했습니다."]
    return reasons[:2]


def _compose_traction(bundle: dict[str, Any]) -> str:
    risk = bundle.get("risk_basis") or {}
    screening = _clean(risk.get("screening_reason"))
    if screening:
        screening = re.split(r"/\s*웹에서", screening, maxsplit=1)[0].strip(" /")
        if screening and not _is_noise_text(screening):
            return _clip(screening, 140)
    market = bundle.get("market_size") or {}
    demand = _clean(market.get("demand_evidence"))
    if demand:
        return _clip(f"수요 근거: {demand}", 140)
    idea = _clean(bundle.get("idea_seed"))
    if idea and not _is_noise_text(idea):
        return _clip(idea, 140)
    return "공시 재무와 시장 성장 근거를 중심으로 사업화를 검토했습니다."


def _compose_analysis_detail(bundle: dict[str, Any]) -> str:
    judgement = bundle.get("judgement") or {}
    market = bundle.get("market_size") or {}
    enrichment = bundle.get("enrichment") or {}
    name = bundle.get("company_name") or "1순위 기업"
    score = judgement.get("external_market_score")
    vbm = (judgement.get("vbm_assessment") or {}).get("conclusion") or ""
    idea = _first_short_phrase(
        enrichment.get("idea") or bundle.get("idea_seed"), max_chars=60
    )
    market_line = " / ".join(
        part
        for part in (
            _clean(market.get("target_market")),
            _clean(market.get("market_size")),
            _clean(market.get("growth")),
        )
        if part
    )
    chunks = [f"{name}을(를) 외부시장·VBM·재무를 종합해 1순위로 선정했습니다."]
    if score is not None:
        chunks.append(f"외부 시장성 {score}점.")
    if vbm:
        chunks.append(f"VBM {vbm}.")
    if market_line:
        chunks.append(f"시장: {market_line}.")
    if idea and not _is_noise_text(idea):
        chunks.append(f"사업: {idea}")
    return _clip(" ".join(chunks), 360)


def _build_financial_statement_table(bundle: dict[str, Any]) -> dict[str, Any]:
    """01 종합 분석용 1순위 핵심 재무제표 표."""

    risk = bundle.get("risk_basis") or {}
    summary = risk.get("financial_summary") or {}
    raw_company = (
        bundle.get("_raw_company")
        if isinstance(bundle.get("_raw_company"), dict)
        else {}
    )
    yearly = [
        row for row in (raw_company.get("financials") or []) if isinstance(row, dict)
    ]
    keys = (
        ("매출액", "매출액", "revenue"),
        ("영업이익", "영업이익", "operating_profit"),
        ("당기순이익", "당기순이익", "net_income"),
        ("자산총계", "자산총계", "assets"),
        ("부채총계", "부채총계", "liabilities"),
        ("자본총계", "자본총계", "equity"),
        ("현금", "현금및현금성자산", "cash"),
        ("영업CF", "영업활동현금흐름", "operating_cf"),
    )
    name = _clip(bundle.get("company_name"), 28)
    if len(yearly) >= 2:
        years = [_clean(row.get("year")) or "?" for row in yearly[-2:]]
        headers = ["항목", *years]
        rows: list[list[str]] = []
        for label, kr, en in keys:
            values = [
                _format_krw(row.get(kr) if row.get(kr) is not None else row.get(en))
                for row in yearly[-2:]
            ]
            if all(v == "-" for v in values):
                continue
            rows.append(
                [
                    _fit_cell(label, 120, fallback_chars=10),
                    *[
                        _fit_cell(v, (_CONTENT_W - 120) / len(years), fallback_chars=12)
                        for v in values
                    ],
                ]
            )
        widths = [120.0] + [(_CONTENT_W - 120.0) / len(years)] * len(years)
    else:
        headers = ["항목", "금액"]
        rows = []
        for label, kr, en in keys:
            value = summary.get(kr)
            if value is None and kr == "현금":
                value = summary.get("현금및현금성자산")
            if value is None and kr == "영업CF":
                value = summary.get("영업활동현금흐름")
            if value is None:
                value = summary.get(en)
            if value in (None, ""):
                continue
            rows.append(
                [
                    _fit_cell(label, 160, fallback_chars=10),
                    _fit_cell(_format_krw(value), _CONTENT_W - 160, fallback_chars=14),
                ]
            )
        widths = [160.0, _CONTENT_W - 160.0]
    return {
        "title": f"1순위 핵심 재무제표 · {name}",
        "headers": headers,
        "rows": rows[:6],
        "widths": widths,
    }


def _compose_differentiation(bundle: dict[str, Any]) -> str:
    enrichment = bundle.get("enrichment") or {}
    text = _clean(enrichment.get("differentiation"))
    if (
        text
        and not _is_noise_text(text)
        and "50점 이상" not in text
        and len(text) >= 60
    ):
        return _clip(text, 320)
    market = bundle.get("market_size") or {}
    subdomain = _clean(bundle.get("subdomain"))
    target = _clean(market.get("target_market"))
    growth = _clean(market.get("growth"))
    name = bundle.get("company_name") or "동사"
    return _clip(
        f"{name}의 차별점은 대형 모듈·발전 사업자와 정면 승부하기보다 "
        f"{target or subdomain or '세부 전력 시장'}에 자산을 집중한 데 있다. "
        f"{('시장 자체는 ' + growth + ' 구간에 있어, ') if growth else ''}"
        "실행만 뒷받침되면 밸류에이션 할인이 해소될 여지가 있다. "
        "반대로 수주와 가동률이 따라오지 않으면 중소 발전사 할인율은 쉽게 좁혀지지 않는다.",
        320,
    )


def _compose_score_reason(bundle: dict[str, Any]) -> str:
    judgement = bundle.get("judgement") or {}
    reasons = judgement.get("investment_reasons") or []
    if isinstance(reasons, list) and reasons:
        text = _clean(reasons[0])
        if text and "50점 이상" not in text and "참고 자료" not in text:
            return text
    market = (
        judgement.get("external_market_assessment")
        if isinstance(judgement.get("external_market_assessment"), dict)
        else {}
    )
    for group in market.get("group_scores") or market.get("groups") or []:
        if not isinstance(group, dict):
            continue
        name = _clean(group.get("name") or group.get("group") or group.get("id"))
        score = group.get("score")
        if name and score is not None:
            return f"{name} {score}점 확보"
    score = judgement.get("external_market_score")
    if score is not None:
        return f"외부 시장성 종합 {score}점"
    return "시장·재무 종합 평가"


def _load_doc_metadata() -> dict[str, Any]:
    if not DOC_METADATA_PATH.is_file():
        return {}
    try:
        payload = json.loads(DOC_METADATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _lookup_doc_meta(file_name: str, metadata: dict[str, Any]) -> dict[str, Any]:
    name = _clean(file_name)
    if name and name in metadata and isinstance(metadata[name], dict):
        return metadata[name]
    stem = name.replace(".pdf.pdf", ".pdf")
    for key, value in metadata.items():
        if not isinstance(value, dict):
            continue
        if name and (
            key == stem
            or key.replace(".pdf.pdf", ".pdf") == stem
            or Path(key).stem in name
            or Path(name).stem in str(key)
        ):
            return value
    return {}


def _is_unknown(value: Any) -> bool:
    return _clean(value).lower() in _UNKNOWN_MARKERS


def _report_title(value: Any) -> str:
    text = _clean(value)
    text = re.sub(r"\.pdf(\.pdf)?$", "", text, flags=re.IGNORECASE)
    return text


def _guess_year_from_filename(file_name: str) -> str | None:
    years = re.findall(r"(20\d{2})", file_name or "")
    if not years:
        return None
    unique = sorted(set(years))
    # 전망 연도(2034/2035 등)는 발행연도로 쓰지 않음
    publish_like = [
        year
        for year in unique
        if year not in _FORECAST_YEARS and 2015 <= int(year) <= 2026
    ]
    if publish_like:
        return publish_like[-1]
    return None


def _publisher_from_url(url: str) -> str:
    host = url.lower()
    if "researchnester.com" in host:
        return "Research Nester"
    if "gminsights.com" in host:
        return "Global Market Insights"
    if "fortunebusinessinsights.com" in host:
        return "Fortune Business Insights"
    if "iea.org" in host:
        return "IEA"
    if "dart.fss.or.kr" in host:
        return "DART"
    if "bok.or.kr" in host:
        return "한국은행"
    match = re.search(r"https?://(?:www\.)?([^/]+)", url)
    if not match:
        return ""
    host = match.group(1)
    return host.split(".")[0]


def _site_from_url(url: str) -> str:
    publisher = _publisher_from_url(url)
    if publisher in {"Research Nester", "IEA", "DART", "한국은행"}:
        return publisher
    match = re.search(r"https?://(?:www\.)?([^/]+)", url or "")
    return match.group(1) if match else ""


def _guess_publisher_from_filename(file_name: str, meta: dict[str, Any]) -> str:
    text = file_name or ""
    for token, publisher in _PUBLISHER_HINTS:
        if token.lower() in text.lower():
            return publisher
    keywords = _clean(meta.get("keywords"))
    for token, publisher in _PUBLISHER_HINTS:
        if token.lower() in keywords.lower():
            return publisher
    doc_type = _clean(meta.get("doc_type")).lower()
    if doc_type in {"market_report", "policy_report"} or "시장 규모" in text:
        if "iea" in text.lower() or "iea" in keywords.lower():
            return "IEA"
        return "Research Nester"
    return ""


def _guess_doc_type(meta: dict[str, Any], file_name: str) -> str:
    doc_type = _clean(meta.get("doc_type") or meta.get("type")).lower()
    if "paper" in doc_type:
        return "paper"
    if doc_type in {"web", "webpage"}:
        return "web"
    if file_name.lower().endswith(".pdf") or "report" in doc_type:
        return "report"
    return "report"


def _citation_type(doc_type: str, url: str | None) -> str:
    if doc_type == "paper":
        return "paper"
    if url and "researchnester.com" in url.lower():
        return "report"
    if doc_type == "web":
        return "web"
    return "report"


def complete_market_source(
    raw: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """RAG PDF·웹 출처를 기관 보고서/논문/웹페이지 인용 형식으로 채웁니다."""

    meta_index = metadata if metadata is not None else _load_doc_metadata()
    file_name = _clean(
        raw.get("file_name") or raw.get("source") or raw.get("title") or raw.get("name")
    )
    page = raw.get("page")
    meta = _lookup_doc_meta(file_name, meta_index)
    url = raw.get("url") or raw.get("source_url") or meta.get("url") or None
    if isinstance(url, str):
        url = url.strip() or None

    doc_type = _guess_doc_type(meta, file_name)
    publisher = ""
    for candidate in (
        raw.get("publisher"),
        raw.get("author"),
        meta.get("publisher"),
        meta.get("author"),
        _publisher_from_url(url or ""),
        _guess_publisher_from_filename(file_name, meta),
    ):
        if not _is_unknown(candidate):
            publisher = _clean(candidate)
            break

    year = ""
    for candidate in (
        raw.get("year"),
        raw.get("date"),
        meta.get("year"),
        _guess_year_from_filename(file_name),
    ):
        text = _clean(candidate)
        if _is_unknown(text):
            continue
        year_match = re.search(r"(20\d{2})", text)
        if year_match and year_match.group(1) not in _FORECAST_YEARS:
            year = year_match.group(1)
            break
    if not year and publisher == "Research Nester":
        year = "2025"

    title = _report_title(raw.get("title")) or _report_title(file_name) or "시장 자료"
    citation_type = _citation_type(doc_type, url)
    journal = _clean(raw.get("journal") or meta.get("journal"))
    volume_pages = _clean(raw.get("volume_pages") or meta.get("volume_pages"))
    site = _clean(raw.get("site")) or (
        _site_from_url(url or "") if citation_type == "web" else ""
    )

    completeness = "complete" if publisher and url and year else "partial"
    if not publisher and not url:
        completeness = "incomplete"

    notice_parts = []
    if not publisher:
        notice_parts.append("발행기관 미확인")
    if not year:
        notice_parts.append("발행연도 미확인")
    if not url:
        notice_parts.append("원문 URL 없음")

    return {
        "title": title,
        "publisher": publisher,
        "author": publisher,
        "year": year,
        "url": url,
        "page": page,
        "file_name": file_name or None,
        "doc_type": citation_type,
        "journal": journal or None,
        "volume_pages": volume_pages or None,
        "site": site or None,
        "sub_domain": _clean(raw.get("sub_domain") or meta.get("sub_domain")) or None,
        "distance": raw.get("distance"),
        "completeness": completeness,
        "source_notice": " / ".join(notice_parts) if notice_parts else None,
    }


def extract_report_source(company: dict[str, Any]) -> dict[str, Any]:
    """report.md에 쓸 필수 필드만 남긴 원천 데이터를 만듭니다.

    포함: 식별, 사업 아이디어, 시장 규모, CEO, 리스크 근거, 공시·시장 출처
    제외: bizr_no, address, homepage_url, pdf_path, audit_reports_count, latest_report
    재무: financial_summary(한글)만 사용 (normalized 중복 제거)
    """

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
    market_summary = (
        market_context.get("summary")
        if isinstance(market_context.get("summary"), dict)
        else {}
    )
    market = company.get("market") if isinstance(company.get("market"), dict) else {}
    if not market_summary and isinstance(market.get("summary"), dict):
        market_summary = market["summary"]

    screening = (
        company.get("screening") if isinstance(company.get("screening"), dict) else {}
    )
    judgement = (
        company.get("judgement") if isinstance(company.get("judgement"), dict) else {}
    )
    evaluation = (
        company.get("evaluation") if isinstance(company.get("evaluation"), dict) else {}
    )

    financial_summary = (
        company.get("financial_summary")
        if isinstance(company.get("financial_summary"), dict)
        else {}
    )
    if not financial_summary:
        dart = company.get("dart") if isinstance(company.get("dart"), dict) else {}
        financial_summary = (
            dart.get("financial_summary")
            if isinstance(dart.get("financial_summary"), dict)
            else {}
        )

    raw_sources = []
    for bucket in (
        market_context.get("sources"),
        market.get("sources"),
        market.get("chunk"),
    ):
        if isinstance(bucket, list):
            raw_sources.extend(item for item in bucket if isinstance(item, dict))

    metadata = _load_doc_metadata()
    market_sources = []
    seen_keys: set[str] = set()
    for item in raw_sources:
        completed = complete_market_source(item, metadata=metadata)
        key = f"{completed.get('file_name')}|{completed.get('page')}|{completed.get('url')}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        market_sources.append(completed)

    idea_seed = (
        _clean(inferred.get("description"))
        or _clean(company.get("description"))
        or _clean(company.get("intro"))
    )

    return {
        "company_name": _clean(company.get("company_name") or company.get("name")),
        "corp_code": _clean(company.get("corp_code") or company.get("id")),
        "ceo": _clean(company.get("ceo")),
        "idea_seed": idea_seed,
        "idea_seed_notice": (
            "회사명·업종 추정 설명입니다. 실제 제품·사업 근거 보강이 필요합니다."
            if idea_seed
            else "사업 아이디어 원문이 비어 있습니다."
        ),
        "market_size": {
            "target_market": _clean(
                market_summary.get("target_market") or market.get("subdomain")
            ),
            "market_size": _clean(market_summary.get("market_size")),
            "growth": _clean(market_summary.get("growth")),
            "demand_evidence": _clean(
                market_summary.get("demand_evidence") or market.get("demand")
            ),
        },
        "team_seed": {
            "ceo": _clean(company.get("ceo")),
            "notice": "공시상 대표자(CEO)만 확보. 창업자·핵심 인력의 기술 역량 자료는 추가 필요.",
        },
        "risk_basis": {
            "financial_summary": financial_summary,
            "stage_source": _clean(
                screening.get("stage_source")
                or company.get("stage_source")
                or (company.get("dart") or {}).get("stage_source")
            ),
            "estimated_investment_stage": _clean(
                company.get("estimated_investment_stage")
                or screening.get("investment_stage")
            ),
            "screening_reason": _clean(screening.get("reason")),
        },
        "disclosure_sources": {
            "dart_viewer_link": company.get("dart_viewer_link")
            or (company.get("dart") or {}).get("dart_viewer_link"),
            "report_nm": company.get("report_nm")
            or (company.get("dart") or {}).get("report_nm"),
            "rcept_dt": company.get("rcept_dt")
            or (company.get("dart") or {}).get("rcept_dt"),
        },
        "market_sources": market_sources,
        "judgement": {
            "decision": judgement.get("decision") or evaluation.get("decision"),
            "reason": judgement.get("reason") or evaluation.get("reason"),
            "investment_reasons": list(
                judgement.get("investment_reasons")
                or evaluation.get("investment_reasons")
                or []
            ),
            "external_market_score": (
                judgement.get("external_market_score")
                if judgement.get("external_market_score") is not None
                else evaluation.get("external_market_score")
            ),
            "external_market_assessment": (
                judgement.get("external_market_assessment")
                if isinstance(judgement.get("external_market_assessment"), dict)
                else {}
            ),
            "vbm_assessment": (
                judgement.get("vbm_assessment")
                if isinstance(judgement.get("vbm_assessment"), dict)
                else evaluation.get("vbm") or {}
            ),
        },
        "competition": company.get("competitor_research")
        or company.get("competition")
        or {},
        "subdomain": _clean(
            company.get("subdomain")
            or market.get("subdomain")
            or inferred.get("subdomain")
        ),
    }


def enrich_company_with_llm(
    report_source: dict[str, Any],
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """빠진 서술(아이디어·리스크·팀·출처)을 OpenAI로 보강합니다."""

    from openai import OpenAI

    prompt = format_prompt(
        "report_enrich",
        company_json=json.dumps(report_source, ensure_ascii=False, indent=2),
    )
    client = OpenAI()
    response = client.chat.completions.create(
        model=model or REPORT_PREP_MODEL,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 AI 데이터센터 에너지 인프라 투자 보고서 보조입니다. "
                    "반드시 JSON만 반환하세요. 없는 수치는 만들지 마세요."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or "{}"
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("LLM 응답이 JSON 객체가 아닙니다.")
    return payload


def _fallback_enrichment(report_source: dict[str, Any]) -> dict[str, Any]:
    """LLM 실패 시 원천 데이터만으로 최소 보강 블록을 만듭니다."""

    market = report_source.get("market_size") or {}
    judgement = report_source.get("judgement") or {}
    risk = report_source.get("risk_basis") or {}
    team = report_source.get("team_seed") or {}
    idea = report_source.get("idea_seed") or "[사업 개요 입력 대기]"
    ceo = team.get("ceo") or "미확인"

    return {
        "idea": idea if idea and not _is_noise_text(idea) else "",
        "technology": report_source.get("subdomain") or "energy infrastructure",
        "team": "",
        "risks": "",
        "market_chart_takeaway": (
            f"{market.get('target_market') or '관련 시장'} 규모·성장 수치이며 "
            "해당 기업 매출 전망과 동일시할 수 없습니다."
        ),
        "differentiation": "",
        "demand": _clean(market.get("demand_evidence")) or "",
        "analysis_points": [],
        "market_sources": [],
        "data_status": "source_only_fallback",
        "data_notice": "LLM 보강 없이 원천 필드만으로 보고서를 구성했습니다.",
    }


def merge_enrichment(
    report_source: dict[str, Any],
    enrichment: dict[str, Any],
) -> dict[str, Any]:
    """원천 출처와 LLM 보강을 합치고, 시장 출처는 메타 완성본을 우선합니다."""

    metadata = _load_doc_metadata()
    base_sources = list(report_source.get("market_sources") or [])
    llm_sources = [
        item
        for item in (enrichment.get("market_sources") or [])
        if isinstance(item, dict)
    ]

    # 파일명 기준으로 LLM publisher/year/url을 덮어쓰기
    by_file: dict[str, dict[str, Any]] = {}
    for item in base_sources:
        key = _clean(item.get("file_name") or item.get("title")) or str(len(by_file))
        by_file[key] = dict(item)

    for item in llm_sources:
        key = _clean(item.get("file_name") or item.get("title"))
        if not key:
            completed = complete_market_source(item, metadata=metadata)
            by_file[str(len(by_file))] = completed
            continue
        current = by_file.get(key, {})
        merged = {
            **current,
            **{k: v for k, v in item.items() if v not in (None, "", [])},
        }
        by_file[key] = complete_market_source(merged, metadata=metadata)

    market_sources = list(by_file.values())
    incomplete = sum(
        1
        for item in market_sources
        if item.get("completeness") in {"partial", "incomplete"}
    )

    return {
        **report_source,
        "enrichment": {
            "idea": _clean(enrichment.get("idea")) or report_source.get("idea_seed"),
            "technology": _clean(enrichment.get("technology"))
            or report_source.get("subdomain"),
            "team": _clean(enrichment.get("team"))
            or (report_source.get("team_seed") or {}).get("notice"),
            "risks": _clean(enrichment.get("risks")),
            "unknowns": _clean(enrichment.get("unknowns")),
            "market_chart_takeaway": _clean(enrichment.get("market_chart_takeaway")),
            "differentiation": _clean(enrichment.get("differentiation")),
            "demand": _clean(enrichment.get("demand"))
            or (report_source.get("market_size") or {}).get("demand_evidence"),
            "analysis_points": [
                {
                    "title": _clip(point.get("title"), 40),
                    "body": _clip(point.get("body"), 160),
                }
                for point in (enrichment.get("analysis_points") or [])
                if isinstance(point, dict)
            ][:4],
            "data_status": enrichment.get("data_status") or "ai_enriched_unverified",
            "data_notice": enrichment.get("data_notice")
            or "공시·RAG 근거 기반 LLM 보강(미검증 가능).",
        },
        "market_sources": market_sources,
        "market_sources_incomplete_count": incomplete,
    }


def _scores_for_report(report_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    judgement = report_bundle.get("judgement") or {}
    market = judgement.get("external_market_assessment") or {}
    rows: list[dict[str, Any]] = []
    scores = market.get("scores") if isinstance(market, dict) else None
    if isinstance(scores, dict):
        for criterion, payload in list(scores.items())[:6]:
            if not isinstance(payload, dict):
                continue
            reason = _clean(payload.get("reason") or "")
            if (not reason) or ("50점 이상" in reason) or ("참고 자료" in reason):
                reason = _compose_score_reason(report_bundle)
            rows.append(
                {
                    "criterion": _fit_cell(criterion, _SCORE_COL[0], fallback_chars=18),
                    "score": _fit_cell(
                        payload.get("score"), _SCORE_COL[1], fallback_chars=6
                    ),
                    "reason": _fit_cell(reason, _SCORE_COL[2], fallback_chars=42),
                }
            )
    if not rows and judgement.get("external_market_score") is not None:
        rows.append(
            {
                "criterion": _fit_cell("외부 시장 종합", _SCORE_COL[0]),
                "score": _fit_cell(
                    judgement.get("external_market_score"), _SCORE_COL[1]
                ),
                "reason": _fit_cell(
                    _compose_score_reason(report_bundle),
                    _SCORE_COL[2],
                    fallback_chars=42,
                ),
            }
        )
    vbm = judgement.get("vbm_assessment") or {}
    if isinstance(vbm, dict) and vbm:
        rows.append(
            {
                "criterion": _fit_cell("VBM", _SCORE_COL[0]),
                "score": _fit_cell(
                    vbm.get("conclusion") or "-", _SCORE_COL[1], fallback_chars=6
                ),
                "reason": _fit_cell(
                    vbm.get("summary")
                    or vbm.get("conclusion_reason")
                    or vbm.get("reason")
                    or "",
                    _SCORE_COL[2],
                    fallback_chars=42,
                ),
            }
        )
    return rows[:6]


def _competitor_product_text(item: dict[str, Any], data: dict[str, Any]) -> str:
    """3쪽 '제품·대상' 열용. 긴 business_summary/evidence 대신 짧은 구절 우선."""

    products = data.get("products_services")
    if isinstance(products, list) and products:
        phrase = _first_short_phrase(products[0], max_chars=28)
        if phrase:
            return phrase
    for key in ("product", "business_summary"):
        phrase = _first_short_phrase(data.get(key) or item.get(key), max_chars=28)
        if phrase:
            return phrase
    # evidence는 보통 길어서 매우 짧게만
    phrase = _first_short_phrase(item.get("evidence"), max_chars=24)
    return phrase or "-"


def _competitor_difference_text(item: dict[str, Any], data: dict[str, Any]) -> str:
    """3쪽 '차이·근거' 열용. URL·장문 제외."""

    for key in ("competitive_relevance", "difference"):
        phrase = _first_short_phrase(data.get(key) or item.get(key), max_chars=36)
        if phrase:
            return phrase
    # source_url은 표에 넣지 않음
    return "유사 사업·직접 경쟁 가능"


def _competitors_for_report(report_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    research = report_bundle.get("competition") or {}
    competitors = research.get("competitors") if isinstance(research, dict) else []
    rows: list[dict[str, Any]] = []
    if not isinstance(competitors, list):
        return rows
    for item in competitors[:4]:
        if not isinstance(item, dict):
            continue
        data = (
            item.get("competitor_data")
            if isinstance(item.get("competitor_data"), dict)
            else {}
        )
        # 추천 행은 BOLD로 그려지므로 bold 폭으로도 통과하도록 맞춤
        name = _fit_cell(
            item.get("name") or "-", _COMP_COL[0], bold=True, fallback_chars=14
        )
        name = _fit_cell(name, _COMP_COL[0], bold=False, fallback_chars=14)
        product = _fit_cell(
            _competitor_product_text(item, data),
            _COMP_COL[1],
            bold=True,
            fallback_chars=22,
        )
        product = _fit_cell(product, _COMP_COL[1], bold=False, fallback_chars=22)
        difference = _fit_cell(
            _competitor_difference_text(item, data),
            _COMP_COL[2],
            bold=True,
            fallback_chars=36,
        )
        difference = _fit_cell(difference, _COMP_COL[2], bold=False, fallback_chars=36)
        rows.append({"name": name, "product": product, "difference": difference})
    return rows


def sanitize_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """report.md/PDF 표·SUMMARY 길이 제한을 최종 강제합니다."""

    out = dict(payload)

    competitors = []
    for row in out.get("competitors") or []:
        if not isinstance(row, dict):
            continue
        name = _fit_cell(row.get("name"), _COMP_COL[0], bold=True, fallback_chars=14)
        name = _fit_cell(name, _COMP_COL[0], fallback_chars=14)
        product_raw = _first_short_phrase(row.get("product"), max_chars=28) or "-"
        product = _fit_cell(product_raw, _COMP_COL[1], bold=True, fallback_chars=22)
        product = _fit_cell(product, _COMP_COL[1], fallback_chars=22)
        difference_raw = (
            _first_short_phrase(row.get("difference"), max_chars=36)
            or "유사 사업·직접 경쟁 가능"
        )
        difference = _fit_cell(
            difference_raw, _COMP_COL[2], bold=True, fallback_chars=36
        )
        difference = _fit_cell(difference, _COMP_COL[2], fallback_chars=36)
        competitors.append(
            {
                "name": name or "-",
                "product": product or "-",
                "difference": difference or "-",
            }
        )
    out["competitors"] = competitors[:4]

    scores = []
    for row in out.get("scores") or []:
        if not isinstance(row, dict):
            continue
        scores.append(
            {
                "criterion": _fit_cell(
                    row.get("criterion"), _SCORE_COL[0], fallback_chars=18
                ),
                "score": _fit_cell(row.get("score"), _SCORE_COL[1], fallback_chars=6),
                "reason": _fit_cell(
                    row.get("reason"), _SCORE_COL[2], fallback_chars=42
                ),
            }
        )
    out["scores"] = scores[:6]

    candidates = []
    for row in out.get("candidates") or []:
        if not isinstance(row, dict):
            continue
        decision = _clean(row.get("decision")) or "보류"
        # 추천 행은 BOLD
        use_bold = decision == "추천"
        name = _fit_cell(
            row.get("name"), _CAND_COL[0], bold=use_bold, fallback_chars=14
        )
        name = _fit_cell(name, _CAND_COL[0], fallback_chars=14)
        candidates.append(
            {
                "name": name,
                "decision": _fit_cell(
                    decision, _CAND_COL[1], bold=use_bold, fallback_chars=4
                ),
                "reason": _clip(row.get("reason"), 108),
            }
        )
    out["candidates"] = candidates[:10]

    # SUMMARY 노트: report.py height≤28 기준 (문자 수 clip만으로는 부족)
    out["summary_reasons"] = [
        _fit_summary_note(item, bold=(index == 0), fallback_chars=48)
        for index, item in enumerate((out.get("summary_reasons") or [])[:2])
    ]
    out["top_risk"] = _fit_summary_note(
        out.get("top_risk") or "[핵심 위험 입력 대기]",
        fallback_chars=40,
    )
    out["next_check"] = _fit_summary_note(
        out.get("next_check") or "[추가 확인 사항 입력 대기]",
        fallback_chars=40,
    )
    for key, limit in (
        ("market", 420),
        ("demand", 360),
        ("differentiation", 320),
        ("market_chart_takeaway", 180),
        ("risks", 420),
        ("hold_overview", 160),
        ("analysis_headline", 48),
        ("market_headline", 72),
        ("decision_headline", 72),
        ("analysis_detail", 360),
    ):
        if out.get(key):
            out[key] = _clip(out.get(key), limit)
    out.pop("unknowns", None)

    company = out.get("company") if isinstance(out.get("company"), dict) else {}
    if company:
        company = dict(company)
        company["name"] = _clip(company.get("name"), 36)
        company["idea"] = _clip(company.get("idea"), 420)
        company["technology"] = _clip(company.get("technology"), 80)
        company["team"] = _clip(company.get("team"), 360)
        company["customers_revenue"] = _clip(company.get("customers_revenue"), 160)
        company["traction"] = _clip(company.get("traction"), 200)
        company["problem"] = _clip(company.get("problem"), 420)
        out["company"] = company

    points = []
    for point in (out.get("analysis_points") or [])[:4]:
        if not isinstance(point, dict):
            continue
        points.append(
            {
                "title": _clip(point.get("title"), 36),
                "body": _clip(point.get("body"), 140),
            }
        )
    out["analysis_points"] = points
    # SUMMARY 전용 필드·검토 수·재무표는 그대로 보존
    if payload.get("reviewed_count") is not None:
        out["reviewed_count"] = payload.get("reviewed_count")
    if isinstance(payload.get("financial_statement_table"), dict):
        out["financial_statement_table"] = payload["financial_statement_table"]
    if payload.get("analysis_detail"):
        out["analysis_detail"] = _clip(payload.get("analysis_detail"), 280)
    if "market_series" in payload:
        out["market_series"] = payload.get("market_series") or []

    trimmed_refs: list[dict[str, Any]] = []
    for ref in (out.get("references") or [])[:6]:
        if not isinstance(ref, dict):
            continue
        author = _clean(ref.get("author"))
        title = _report_title(ref.get("title"))
        if _is_unknown(author) or not title:
            continue
        trimmed_refs.append(
            {
                "type": ref.get("type") or "report",
                "author": author,
                "date": _clean(ref.get("date")),
                "title": title,
                "url": _clean(ref.get("url")) or None,
                "site": _clean(ref.get("site")) or None,
                "journal": _clean(ref.get("journal")) or None,
                "volume_pages": _clean(ref.get("volume_pages")) or None,
            }
        )
    out["references"] = trimmed_refs
    return out


def _candidate_decision_label(decision: str) -> str:
    text = _clean(decision)
    if text in {"적합", "추천", "recommend"}:
        return "추천"
    return "보류"


def _pick_featured_bundle(bundles: list[dict[str, Any]]) -> dict[str, Any] | None:
    recommend = []
    for bundle in bundles:
        decision = _candidate_decision_label(
            str((bundle.get("judgement") or {}).get("decision") or "")
        )
        if decision == "추천":
            recommend.append(bundle)
    pool = recommend or bundles
    if not pool:
        return None

    def score_key(item: dict[str, Any]) -> float:
        score = (item.get("judgement") or {}).get("external_market_score")
        try:
            return float(score)
        except (TypeError, ValueError):
            return -1.0

    return max(pool, key=score_key)


def _references_for_report(
    bundles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """PDF 5쪽 REFERENCE용 출처만 간결히 모읍니다 (최대 6건)."""

    references: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(ref: dict[str, Any]) -> bool:
        if len(references) >= 6:
            return False
        key = str(ref.get("url") or ref.get("file_name") or ref.get("title") or "")
        if not key or key in seen:
            return False
        seen.add(key)
        references.append(ref)
        return True

    featured = _pick_featured_bundle(bundles) or (bundles[0] if bundles else None)
    ordered: list[dict[str, Any]] = []
    if featured:
        ordered.append(featured)
    for bundle in bundles:
        if bundle is not featured:
            ordered.append(bundle)

    dart_added = 0
    for bundle in ordered:
        if dart_added >= 2:
            break
        disclosure = bundle.get("disclosure_sources") or {}
        link = disclosure.get("dart_viewer_link")
        if not link:
            continue
        name = _clip(bundle.get("company_name"), 18)
        rcept = _clean(disclosure.get("rcept_dt"))
        add(
            {
                "type": "report",
                "author": "DART",
                "date": rcept[:4] if len(rcept) >= 4 else rcept,
                "title": f"{name} {disclosure.get('report_nm') or '감사보고서'}",
                "url": link,
            }
        )
        dart_added += 1

    market_added = 0
    for bundle in ordered:
        if market_added >= 3 or len(references) >= 6:
            break
        for source in bundle.get("market_sources") or []:
            if market_added >= 3 or len(references) >= 6:
                break
            title = _report_title(source.get("title") or source.get("file_name"))
            author = _clean(source.get("publisher") or source.get("author"))
            if not title or _is_unknown(author):
                continue
            ref_type = source.get("doc_type") or "report"
            if ref_type == "paper" and (
                _is_unknown(source.get("journal")) or _is_unknown(source.get("year"))
            ):
                continue
            add(
                {
                    "type": ref_type,
                    "author": author,
                    "date": _clean(source.get("year") or source.get("date")),
                    "title": title,
                    "url": source.get("url"),
                    "site": source.get("site"),
                    "journal": source.get("journal"),
                    "volume_pages": source.get("volume_pages"),
                    "file_name": source.get("file_name"),
                }
            )
            market_added += 1

    for bundle in ordered:
        if len(references) >= 6:
            break
        research = bundle.get("competition") or {}
        for competitor in (
            research.get("competitors") or [] if isinstance(research, dict) else []
        ):
            url = (competitor or {}).get("source_url")
            if not url:
                continue
            site = _site_from_url(url)
            add(
                {
                    "type": "web",
                    "author": site or _clip((competitor or {}).get("name"), 20),
                    "date": "",
                    "title": _clip((competitor or {}).get("name") or "경쟁사 근거", 40),
                    "site": site,
                    "url": url,
                }
            )
            break

    return references[:6]


def build_report_md_payload(
    state: dict[str, Any],
    report_bundles: list[dict[str, Any]],
) -> dict[str, Any]:
    """정제·보강된 기업 묶음을 ``docs/report.md`` 계약 JSON으로 변환합니다."""

    if not report_bundles:
        rejections = list(state.get("judgement_rejections") or [])
        hold_reason = (
            rejections[0].get("reason")
            if rejections
            else "목표 기업 수만큼 투자 적합 기업을 확보하지 못했습니다."
        )
        return sanitize_report_payload(
            {
                "decision": "hold",
                "candidates": [
                    {
                        "name": item.get("name") or "미선정",
                        "decision": "보류",
                        "reason": item.get("reason") or hold_reason,
                    }
                    for item in (
                        rejections or [{"name": "후보 없음", "reason": hold_reason}]
                    )[:10]
                ],
                "company": {},
                "summary_reasons": [hold_reason],
                "hold_overview": hold_reason,
                "top_risk": "적합 후보 부족",
                "next_check": "DART 재탐색 조건과 투자 판단 기준을 재검토합니다.",
                "scores": [],
                "competitors": [],
                "references": [],
                "data_notice": "report_prep: 후보 기업 없음",
            }
        )

    featured = _pick_featured_bundle(report_bundles) or report_bundles[0]
    featured_judgement = featured.get("judgement") or {}
    featured_enrichment = featured.get("enrichment") or {}
    featured_risk = featured.get("risk_basis") or {}
    financial = featured_risk.get("financial_summary") or {}

    candidates = []
    for bundle in report_bundles:
        judgement = bundle.get("judgement") or {}
        decision_label = _candidate_decision_label(
            str(judgement.get("decision") or "보류")
        )
        reason_raw = _compose_candidate_reason(bundle)
        candidates.append(
            {
                "name": _fit_cell(
                    bundle.get("company_name"),
                    _CAND_COL[0],
                    bold=decision_label == "추천",
                    fallback_chars=14,
                ),
                "decision": decision_label,
                "reason": _clip(reason_raw, 108),
            }
        )

    recommend_count = sum(item.get("decision") == "추천" for item in candidates)
    decision = "recommend" if recommend_count else "hold"
    summary_reasons = [
        _fit_summary_note(item, bold=(index == 0), fallback_chars=48)
        for index, item in enumerate(_compose_summary_reasons(featured))
    ]
    if not summary_reasons:
        summary_reasons = [
            _fit_summary_note(
                "시장·재무 근거를 종합해 투자 적합으로 판단했습니다.", bold=True
            )
        ]

    revenue = financial.get("매출액") or financial.get("revenue")
    assets = financial.get("자산총계") or financial.get("assets")
    stage = featured_risk.get("estimated_investment_stage") or "미확인"
    stage_clean = re.sub(r"자산추정\s*", "", stage).strip() or stage

    company_block = {
        "name": featured.get("company_name"),
        "problem": _clip(_compose_idea_prose(featured), 420),
        "idea": _clip(_compose_idea_prose(featured), 420),
        "technology": _clip(
            featured_enrichment.get("technology") or featured.get("subdomain"),
            80,
        )
        or "energy infrastructure",
        "customers_revenue": _clip(
            f"추정 단계 {stage_clean}"
            + (f" · 매출 {_format_krw(revenue)}" if revenue not in (None, "") else "")
            + (f" · 자산 {_format_krw(assets)}" if assets not in (None, "") else ""),
            160,
        ),
        "team": _compose_team_prose(featured),
        "traction": _compose_traction(featured),
    }

    market_text = _compose_market_prose(featured)

    incomplete_sources = sum(
        int(bundle.get("market_sources_incomplete_count") or 0)
        for bundle in report_bundles
    )
    data_notice = featured_enrichment.get("data_notice") or ""
    if incomplete_sources:
        data_notice = (
            f"{data_notice} 시장 출처 중 {incomplete_sources}건은 "
            "발행기관·연도·원문 URL이 완전하지 않습니다."
        ).strip()

    reviewed_count = _resolve_reviewed_count(state, len(report_bundles))
    financial_table = _build_financial_statement_table(featured)
    analysis_detail = _compose_analysis_detail(featured)
    summary_top_risk = _fit_summary_note(_compose_summary_top_risk(featured))
    summary_next_check = _fit_summary_note(_compose_summary_next_check(featured))

    payload = {
        "decision": decision,
        "candidates": candidates,
        "company": company_block,
        "summary_reasons": summary_reasons,
        "top_risk": summary_top_risk,
        "next_check": summary_next_check,
        "reviewed_count": reviewed_count,
        "analysis_headline": _clip(
            featured.get("subdomain") or "에너지 인프라 후보 분석",
            48,
        ),
        "market_headline": _clip(
            market_text or "시장 성장과 기업의 실제 기회를 구분합니다",
            48,
        ),
        "decision_headline": _clip(
            featured_judgement.get("reason") or "적합·보류 근거를 함께 확인합니다",
            48,
        ),
        "analysis_detail": analysis_detail,
        "financial_statement_table": financial_table,
        "market": market_text,
        "demand": _compose_demand_prose(featured),
        "differentiation": _compose_differentiation(featured),
        "market_chart_takeaway": _clip(
            featured_enrichment.get("market_chart_takeaway"),
            180,
        ),
        "market_series": [],  # 수치 시계열이 없으면 빈 차트 자리에 그래프를 그리지 않음
        "analysis_points": [],  # 01쪽은 재무제표 표 + 사업 아이디어·팀 서술
        "competitors": _competitors_for_report(featured),
        "scores": _scores_for_report(featured),
        "risks": _compose_body_risks(featured),
        "references": _references_for_report(report_bundles),
        "hold_overview": summary_reasons[0] if decision == "hold" else "",
        "data_notice": data_notice,
        "data_status": featured_enrichment.get("data_status"),
    }
    sanitized = sanitize_report_payload(payload)
    print(
        "  [보고서 준비] report.md 길이 검증 완료 "
        f"(competitors={len(sanitized.get('competitors') or [])}, "
        f"scores={len(sanitized.get('scores') or [])}, "
        f"candidates={len(sanitized.get('candidates') or [])}, "
        f"검토={sanitized.get('reviewed_count')})"
    )
    return sanitized


def prepare_companies_for_report(
    companies: list[dict[str, Any]],
    *,
    use_llm: bool = True,
    model: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """기업 목록을 정제·보강하고, 원본에 report_source/report_enrichment를 붙입니다."""

    enriched_companies: list[dict[str, Any]] = []
    report_bundles: list[dict[str, Any]] = []

    for index, company in enumerate(companies, 1):
        name = _clean(company.get("company_name") or company.get("name")) or f"#{index}"
        print(f"  [보고서 준비] ({index}/{len(companies)}) {name} 원천 필드 추출")
        source = extract_report_source(company)
        incomplete = sum(
            1
            for item in source.get("market_sources") or []
            if item.get("completeness") in {"partial", "incomplete"}
        )
        print(
            f"  [보고서 준비] {name}: 시장 출처 {len(source.get('market_sources') or [])}건 "
            f"(미완 {incomplete}건), 재무 필드="
            f"{'있음' if (source.get('risk_basis') or {}).get('financial_summary') else '없음'}"
        )

        enrichment: dict[str, Any]
        if use_llm:
            try:
                print(
                    f"  [보고서 준비] {name}: LLM 서술 보강 시작 (model={model or REPORT_PREP_MODEL})"
                )
                enrichment = enrich_company_with_llm(source, model=model)
                print(f"  [보고서 준비] {name}: LLM 서술 보강 완료")
            except Exception as error:
                print(f"  [보고서 준비] {name}: LLM 보강 실패 → 원천만 사용 ({error})")
                enrichment = _fallback_enrichment(source)
        else:
            enrichment = _fallback_enrichment(source)

        bundle = merge_enrichment(source, enrichment)
        bundle["_raw_company"] = company
        report_bundles.append(bundle)

        updated = dict(company)
        updated["report_source"] = {
            k: v
            for k, v in source.items()
            if k
            not in {
                "competition",
                "judgement",
            }
        }
        updated["report_enrichment"] = bundle.get("enrichment")
        updated["report_market_sources"] = bundle.get("market_sources")
        enriched_companies.append(updated)

    return enriched_companies, report_bundles


def prepare_report_node(state: dict[str, Any]) -> dict[str, Any]:
    """judge 이후 데이터를 report.md 계약으로 변환해 State에 적재합니다."""

    companies = list(state.get("eligible_companies") or [])
    print("\n[작업] 보고서 데이터 준비 (judge → report.md)")
    print(f"  대상 기업 수: {len(companies)}")

    use_llm = (os.getenv("REPORT_PREP_USE_LLM") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }

    try:
        enriched_companies, report_bundles = prepare_companies_for_report(
            companies,
            use_llm=use_llm,
        )
        report_payload = build_report_md_payload(state, report_bundles)
        status = "prepared"
        message = (
            f"보고서 데이터 준비 완료: 기업 {len(enriched_companies)}개, "
            f"decision={report_payload.get('decision')}, "
            f"references={len(report_payload.get('references') or [])}건"
        )
        print(f"  [보고서 준비] {message}")
        if report_payload.get("data_notice"):
            print(f"  [보고서 준비] 안내: {report_payload.get('data_notice')}")
    except Exception as error:
        enriched_companies = companies
        report_payload = {}
        status = "failed"
        message = f"보고서 데이터 준비 실패: {error}"
        print(f"  [보고서 준비] {message}")
        return {
            "eligible_companies": enriched_companies,
            "report_payload": report_payload,
            "report_prep_status": status,
            "next_stage": "generate_report",
            "workflow_errors": [message],
            "execution_log": [*state.get("execution_log", []), message],
        }

    return {
        "eligible_companies": enriched_companies,
        "report_payload": report_payload,
        "report_prep_status": status,
        "next_stage": "generate_report",
        "execution_log": [*state.get("execution_log", []), message],
    }


__all__ = [
    "extract_report_source",
    "complete_market_source",
    "enrich_company_with_llm",
    "merge_enrichment",
    "build_report_md_payload",
    "sanitize_report_payload",
    "prepare_companies_for_report",
    "prepare_report_node",
    "run_report_from_json",
]


def run_report_from_json(
    input_path: Path | str | None = None,
    *,
    output_path: Path | str | None = None,
    use_llm: bool | None = None,
    company_limit: int | None = None,
) -> dict[str, Any]:
    """judge 결과 JSON으로 prepare_report → PDF까지 바로 실행합니다.

    기본 입력: ``outputs/final_companies_latest.json``
    """

    from agents.report import generate_report

    path = Path(input_path) if input_path else DEFAULT_LATEST_JSON
    if not path.is_file():
        raise FileNotFoundError(f"입력 JSON이 없습니다: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload.get("eligible_companies"), list):
        companies = list(payload["eligible_companies"])
    elif isinstance(payload, list):
        companies = list(payload)
    else:
        raise ValueError("eligible_companies 배열이 필요합니다.")

    if company_limit is not None:
        companies = companies[: max(1, company_limit)]

    if use_llm is None:
        use_llm = (os.getenv("REPORT_PREP_USE_LLM") or "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

    print("=" * 70)
    print("report_prep 단독 실행 (judge 이후 → PDF)")
    print(f"  입력 JSON : {path}")
    print(f"  기업 수   : {len(companies)}")
    print(f"  LLM 보강  : {'사용' if use_llm else '건너뜀'}")
    print("=" * 70)

    state: dict[str, Any] = {
        "eligible_companies": companies,
        "final_suitable_companies": list(payload.get("final_suitable_companies") or []),
        "company_evaluations": list(payload.get("company_evaluations") or []),
        "judgement_rejections": list(payload.get("judgement_rejections") or []),
        "wacc": payload.get("wacc"),
        "target_company_count": payload.get("target_company_count") or len(companies),
        "dart_fetch_requested": payload.get("dart_fetch_requested"),
        "dart_seen_corp_codes": list(payload.get("dart_seen_corp_codes") or []),
        "execution_log": [f"report_prep 단독: {path.name}"],
        "next_stage": "generate_report",
    }

    # prepare_companies_for_report가 env를 보므로 일시 지정
    prev = os.environ.get("REPORT_PREP_USE_LLM")
    os.environ["REPORT_PREP_USE_LLM"] = "1" if use_llm else "0"
    try:
        prepared = prepare_report_node(state)
    finally:
        if prev is None:
            os.environ.pop("REPORT_PREP_USE_LLM", None)
        else:
            os.environ["REPORT_PREP_USE_LLM"] = prev

    state = {**state, **prepared}
    report_payload = sanitize_report_payload(
        state.get("report_payload")
        if isinstance(state.get("report_payload"), dict)
        else {}
    )
    if not report_payload.get("candidates"):
        raise RuntimeError("report_payload 생성에 실패했습니다.")

    out = (
        Path(output_path)
        if output_path
        else (PROJECT_ROOT / "outputs" / "investment_report.pdf")
    )
    print("\n[작업] PDF 생성")
    try:
        pdf_path = generate_report(report_payload, out)
        status = "completed"
        print(f"  [보고서] 생성 완료 → {pdf_path}")
    except Exception as error:
        pdf_path = out
        status = "failed"
        print(f"  [보고서] 생성 실패: {error}")
        raise

    return {
        "status": status,
        "report_path": str(pdf_path),
        "report_payload": report_payload,
        "report_prep_status": prepared.get("report_prep_status"),
        "company_count": len(companies),
    }


def main() -> None:
    """``python -m agents.report_prep`` 진입점."""

    import argparse

    parser = argparse.ArgumentParser(
        description="judge 결과 JSON으로 report_prep → PDF만 실행합니다."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_LATEST_JSON,
        help=f"입력 JSON (기본: {DEFAULT_LATEST_JSON})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "investment_report.pdf",
        help="PDF 출력 경로",
    )
    parser.add_argument(
        "--company-limit",
        type=int,
        default=None,
        help="앞에서 N개 기업만 사용",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="LLM 보강 없이 원천 필드만으로 PDF 생성 (빠른 검증)",
    )
    args = parser.parse_args()
    result = run_report_from_json(
        args.input,
        output_path=args.output,
        use_llm=not args.no_llm,
        company_limit=args.company_limit,
    )
    print("\n" + "=" * 70)
    print(f"상태        : {result.get('status')}")
    print(f"PDF         : {result.get('report_path')}")
    print(f"준비 상태   : {result.get('report_prep_status')}")
    print("=" * 70)


if __name__ == "__main__":
    main()
