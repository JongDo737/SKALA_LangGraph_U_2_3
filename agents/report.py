# 작성자: 김가빈
# 파일 설명: 투자 평가 결과를 고정 5쪽 한국어 PDF로 출력한다.

"""투자 평가 결과를 고정 5쪽 한국어 PDF로 출력한다.

python -m agents.report --input /path/to/report_state.json --output outputs/report.pdf
"""
from __future__ import annotations

import argparse
import json
import re
from html import escape
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

W, H = A4
L, R = 48, W - 48
FONT = "Pretendard"
BOLD = "PretendardSemiBold"
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
INK = colors.HexColor("#172F3D")
MUTED = colors.HexColor("#64727A")
LINE = colors.HexColor("#DCE2E3")
PALE = colors.HexColor("#F6F5F1")
ACCENT = colors.HexColor("#A66B38")
SOFT_BLUE = colors.HexColor("#EEF2F3")


def _font(path: str | None) -> None:
    regular = Path(path) if path else FONT_DIR / "Pretendard-Regular.ttf"
    semibold = FONT_DIR / "Pretendard-SemiBold.ttf"
    if not regular.is_file():
        raise RuntimeError(
            "Pretendard TTF를 찾지 못했습니다. --font 경로를 지정하세요."
        )
    pdfmetrics.registerFont(TTFont(FONT, str(regular)))
    pdfmetrics.registerFont(
        TTFont(BOLD, str(semibold if semibold.is_file() else regular))
    )


def _v(value: Any) -> str:
    return "확인되지 않음" if value in (None, "", []) else str(value)


class PageWriter:
    def __init__(self, path: Path, font_path: str | None):
        _font(font_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.c = canvas.Canvas(str(path), pagesize=A4)
        self.c.setTitle("AI 스타트업 투자 평가 보고서")
        self.page = 0
        self.y = 0.0

    def start(self, title: str, subtitle: str) -> None:
        if self.page:
            self._footer()
            self.c.showPage()
        self.page += 1
        self.c.setFillColor(INK)
        self.c.rect(0, H - 6, W, 6, fill=1, stroke=0)
        self.c.setFillColor(MUTED)
        self.c.setFont(FONT, 8)
        self.c.drawString(L, H - 42, "COMPREHENSIVE RESEARCH  /  ENERGY INFRASTRUCTURE")
        self.c.drawRightString(R, H - 42, "INVESTMENT REPORT")
        self.c.setFillColor(INK)
        if self.page == 1 and title == "SUMMARY":
            self.c.setFillColor(ACCENT)
            self.c.setFont(BOLD, 9)
            self.c.drawString(L, H - 67, "SUMMARY")
            self.c.setFillColor(INK)
            self.c.setFont(BOLD, 18)
            self.c.drawString(L, H - 94, "AI 데이터센터 전력 인프라")
            self.c.drawString(L, H - 119, "스타트업 투자 분석")
            self.y = H - 137
        else:
            self.c.setFont(BOLD, 23)
            self.c.drawString(L, H - 79, title)
            self.y = H - 101
            if subtitle:
                clipped = str(subtitle).strip()
                if len(clipped) > 72:
                    clipped = clipped[:69].rstrip() + "..."
                headline = Paragraph(
                    escape(clipped),
                    ParagraphStyle(
                        "chapter_headline",
                        fontName=BOLD,
                        fontSize=10.5,
                        leading=15,
                        textColor=INK,
                        wordWrap="CJK",
                    ),
                )
                _, height = headline.wrap(R - L, H)
                # 두 줄을 넘으면 더 짧게 잘라 한 번 더 맞춥니다.
                if height > 30:
                    clipped = clipped[:48].rstrip() + "..."
                    headline = Paragraph(
                        escape(clipped),
                        ParagraphStyle(
                            "chapter_headline",
                            fontName=BOLD,
                            fontSize=10.5,
                            leading=15,
                            textColor=INK,
                            wordWrap="CJK",
                        ),
                    )
                    _, height = headline.wrap(R - L, H)
                headline.drawOn(self.c, L, self.y - height)
                self.y -= height + 11
        self.c.setStrokeColor(LINE)
        self.c.line(L, self.y, R, self.y)
        self.y -= 23

    def _footer(self) -> None:
        self.c.setStrokeColor(LINE)
        self.c.line(L, 46, R, 46)
        self.c.setFillColor(MUTED)
        self.c.setFont(FONT, 8)
        self.c.drawString(L, 30, "사실 · 추정 · 미확인 사항을 구분해 기재")
        self.c.drawRightString(R, 30, f"{self.page} / 5")

    def _fit(self, height: float) -> None:
        if self.y - height < 66:
            raise ValueError(f"{self.page}쪽 내용이 넘칩니다. 해당 입력을 요약하세요.")

    def text(
        self,
        value: Any,
        size: float = 10,
        color=INK,
        gap: float = 12,
        indent: float = 0,
    ) -> None:
        p = Paragraph(
            escape(_v(value)).replace("\n", "<br/>"),
            ParagraphStyle(
                "body",
                fontName=FONT,
                fontSize=size,
                leading=size * 1.55,
                textColor=color,
                wordWrap="CJK",
            ),
        )
        width = R - L - indent
        _, height = p.wrap(width, H)
        self._fit(height + gap)
        p.drawOn(self.c, L + indent, self.y - height)
        self.y -= height + gap

    def heading(self, label: str) -> None:
        self._fit(28)
        self.c.setFillColor(INK)
        self.c.setFont(BOLD, 11)
        self.c.drawString(L, self.y - 10, label)
        self.c.setStrokeColor(LINE)
        self.c.line(L, self.y - 17, R, self.y - 17)
        self.y -= 31

    def field(self, label: str, value: Any) -> None:
        self._fit(26)
        self.c.setFillColor(MUTED)
        self.c.setFont(FONT, 9)
        self.c.drawString(L, self.y - 10, label)
        self.text(value, 9, INK, 9, 100)
        self.c.setStrokeColor(LINE)
        self.c.line(L, self.y + 4, R, self.y + 4)
        self.y -= 10

    def table(
        self,
        headers: list[str],
        rows: list[list[Any]],
        widths: list[float],
        height: float = 25,
    ) -> None:
        self._fit((len(rows) + 1) * height + 12)
        self.c.setFillColor(INK)
        self.c.rect(L, self.y - height, R - L, height, fill=1, stroke=0)
        for index, row in enumerate([headers, *rows]):
            if index and len(row) > 1 and row[1] == "추천":
                self.c.setFillColor(SOFT_BLUE)
                self.c.rect(L, self.y - height, R - L, height, fill=1, stroke=0)
            x = L
            for value, width in zip(row, widths):
                original = _v(value).replace("\n", " ")
                face = (
                    BOLD
                    if index == 0 or (index and len(row) > 1 and row[1] == "추천")
                    else FONT
                )
                if pdfmetrics.stringWidth(original, face, 8) > width - 12:
                    raise ValueError(
                        f"{self.page}쪽 표의 '{original[:24]}' 셀이 너무 깁니다. "
                        "요약 필드를 짧게 작성하세요."
                    )
                self.c.setFont(face, 8)
                self.c.setFillColor(colors.white if index == 0 else INK)
                self.c.drawString(x + 6, self.y - 16, original)
                x += width
            if index:
                self.c.setStrokeColor(LINE)
                self.c.line(L, self.y - height, R, self.y - height)
            self.y -= height
        self.y -= 12

    def analysis_block(self, number: int, title: str, body: str) -> None:
        """결론·근거를 한 행에 담는 간결한 리서치 메모 블록."""
        x = L + 43
        width = R - x
        paragraph = Paragraph(
            escape(_v(body)).replace("\n", "<br/>"),
            ParagraphStyle(
                "analysis",
                fontName=FONT,
                fontSize=9.5,
                leading=15,
                textColor=INK,
                wordWrap="CJK",
            ),
        )
        _, text_height = paragraph.wrap(width, H)
        block_height = max(54, text_height + 34)
        self._fit(block_height)
        self.c.setFillColor(ACCENT)
        self.c.setFont(BOLD, 16)
        self.c.drawString(L, self.y - 18, f"{number:02d}")
        self.c.setFillColor(INK)
        self.c.setFont(BOLD, 10.5)
        self.c.drawString(x, self.y - 14, title)
        paragraph.drawOn(self.c, x, self.y - 25 - text_height)
        self.c.setStrokeColor(LINE)
        self.c.line(L, self.y - block_height + 5, R, self.y - block_height + 5)
        self.y -= block_height

    def market_chart(
        self,
        label: str,
        unit: str,
        series: list[dict[str, Any]],
        source: str,
        takeaway: str = "",
    ) -> None:
        """0을 기준으로 실제값과 전망값을 구분하는 시장 지표 막대그래프."""
        if not series:
            self._fit(112)
            self.c.setFillColor(PALE)
            self.c.rect(L, self.y - 100, R - L, 100, fill=1, stroke=0)
            self.c.setFillColor(MUTED)
            self.c.setFont(FONT, 9)
            self.c.drawString(
                L + 17, self.y - 53, "[시장 지표와 출처가 전달되면 그래프 표시]"
            )
            self.y -= 112
            return
        if len(series) > 5:
            raise ValueError("market_series는 최대 5개입니다.")
        if not label or not unit or not source:
            raise ValueError("시장 그래프에는 지표명·단위·출처 번호가 필요합니다.")
        for item in series:
            number = item.get("value")
            if (
                not isinstance(number, (int, float))
                or isinstance(number, bool)
                or number < 0
                or not item.get("period")
                or item.get("kind") not in ("actual", "forecast")
            ):
                raise ValueError(
                    "market_series에는 기간, 0 이상 수치, actual/forecast 구분이 필요합니다."
                )
        maximum = max(item["value"] for item in series)
        if maximum == 0:
            raise ValueError(
                "시장 그래프 수치가 모두 0이면 그래프를 표시할 수 없습니다."
            )
        needed = 53 + len(series) * 31 + (22 if takeaway else 0)
        self._fit(needed)
        self.c.setStrokeColor(INK)
        self.c.line(L, self.y - 1, R, self.y - 1)
        self.c.setFillColor(INK)
        self.c.setFont(BOLD, 10)
        self.c.drawString(L + 12, self.y - 18, label)
        self.c.setFillColor(MUTED)
        self.c.setFont(FONT, 8)
        self.c.drawRightString(R - 12, self.y - 18, f"단위: {unit}")
        y = self.y - 38
        bar_x, bar_w = L + 105, R - L - 185
        for item in series:
            caption = str(item["period"]) + (
                " 전망" if item["kind"] == "forecast" else " 실적"
            )
            if pdfmetrics.stringWidth(caption, FONT, 8) > 88:
                raise ValueError("시장 지표 기간 표기가 너무 깁니다.")
            self.c.setFillColor(INK)
            self.c.setFont(FONT, 8)
            self.c.drawString(L + 12, y - 8, caption)
            self.c.setFillColor(LINE)
            self.c.roundRect(bar_x, y - 10, bar_w, 9, 3, fill=1, stroke=0)
            self.c.setFillColor(INK if item["kind"] == "actual" else ACCENT)
            self.c.roundRect(
                bar_x, y - 10, bar_w * item["value"] / maximum, 9, 3, fill=1, stroke=0
            )
            self.c.setFillColor(INK)
            self.c.setFont(BOLD, 8)
            self.c.drawRightString(R - 12, y - 8, f"{item['value']:g}")
            y -= 31
        self.c.setFillColor(MUTED)
        self.c.setFont(FONT, 7.5)
        self.c.drawString(L + 12, y + 1, f"출처: {source}  |  막대는 0부터 시작")
        if takeaway:
            if pdfmetrics.stringWidth(takeaway, BOLD, 9) > R - L - 24:
                raise ValueError("시장 그래프 해석 문장이 너무 깁니다.")
            self.c.setFillColor(INK)
            self.c.setFont(BOLD, 9)
            self.c.drawString(L + 12, y - 20, takeaway)
        self.c.setStrokeColor(LINE)
        self.c.line(L, self.y - needed + 11, R, self.y - needed + 11)
        self.y -= needed

    def save(self) -> None:
        self._footer()
        self.c.save()


def _reference(item: dict[str, Any]) -> str:
    kind = item.get("type", "web")
    lead = f"{_v(item.get('author'))}({_v(item.get('date'))}). {_v(item.get('title'))}."
    if kind == "paper":
        return f"{lead} {_v(item.get('journal'))}, {_v(item.get('volume_pages'))}."
    if kind == "report":
        return f"{lead} {_v(item.get('url'))}"
    return f"{lead} {_v(item.get('site'))}, {_v(item.get('url'))}"


def _company_name(company: dict[str, Any]) -> str:
    return str(company.get("name") or company.get("company_name") or "이름 없음")


def _candidate_decision_label(decision: str) -> str:
    if decision in {"적합", "추천", "recommend"}:
        return "추천"
    return "보류"


def _pick_featured(companies: list[dict[str, Any]]) -> dict[str, Any] | None:
    scored: list[tuple[float, dict[str, Any]]] = []
    for company in companies:
        judgement = (
            company.get("judgement")
            if isinstance(company.get("judgement"), dict)
            else {}
        )
        score = judgement.get("external_market_score")
        try:
            numeric = float(score) if score is not None else -1.0
        except (TypeError, ValueError):
            numeric = -1.0
        scored.append((numeric, company))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored else None


def _company_detail(company: dict[str, Any]) -> dict[str, Any]:
    market = company.get("market") if isinstance(company.get("market"), dict) else {}
    screening = (
        company.get("screening") if isinstance(company.get("screening"), dict) else {}
    )
    financial = (
        company.get("financial_summary")
        if isinstance(company.get("financial_summary"), dict)
        else {}
    )
    description = (
        company.get("description")
        or market.get("description")
        or company.get("intro")
        or ""
    )
    stage = (
        company.get("estimated_investment_stage")
        or screening.get("investment_stage")
        or ""
    )
    revenue = financial.get("매출액") or financial.get("revenue")
    assets = financial.get("자산총계") or financial.get("total_assets")
    return {
        "name": _company_name(company),
        "problem": market.get("problem")
        or f"{_company_name(company)}가 속한 에너지·인프라 세부 시장의 구조적 병목을 검토했습니다.",
        "idea": description[:280] or "[사업 개요 입력 대기]",
        "technology": market.get("technology")
        or company.get("subdomain")
        or market.get("subdomain")
        or "energy infrastructure",
        "customers_revenue": (
            f"추정 단계 {stage or '미확인'}"
            + (f" / 매출액 {revenue}" if revenue not in (None, "") else "")
            + (f" / 자산총계 {assets}" if assets not in (None, "") else "")
        ),
        "team": company.get("ceo") or "[경영진 정보 입력 대기]",
        "traction": screening.get("reason")
        or judgement_reason(company)
        or "[확보된 트랙션 근거 입력 대기]",
    }


def judgement_reason(company: dict[str, Any]) -> str:
    judgement = (
        company.get("judgement") if isinstance(company.get("judgement"), dict) else {}
    )
    reasons = judgement.get("investment_reasons") or []
    if isinstance(reasons, list) and reasons:
        return str(reasons[0])
    return str(judgement.get("reason") or "")


def _scores_from_company(company: dict[str, Any]) -> list[dict[str, Any]]:
    judgement = (
        company.get("judgement") if isinstance(company.get("judgement"), dict) else {}
    )
    market = judgement.get("external_market_assessment") or {}
    scores = market.get("scores") if isinstance(market, dict) else None
    rows: list[dict[str, Any]] = []
    if isinstance(scores, dict):
        for criterion, payload in list(scores.items())[:6]:
            if not isinstance(payload, dict):
                continue
            rows.append(
                {
                    "criterion": criterion,
                    "score": payload.get("score"),
                    "reason": payload.get("reason") or "",
                }
            )
    if rows:
        return rows
    if judgement.get("external_market_score") is not None:
        rows.append(
            {
                "criterion": "외부 시장 종합",
                "score": judgement.get("external_market_score"),
                "reason": judgement.get("reason") or "",
            }
        )
    vbm = (
        judgement.get("vbm_assessment")
        if isinstance(judgement.get("vbm_assessment"), dict)
        else {}
    )
    if vbm:
        rows.append(
            {
                "criterion": "VBM",
                "score": vbm.get("conclusion") or "-",
                "reason": vbm.get("summary") or vbm.get("reason") or "",
            }
        )
    return rows[:6]


def _competitors_from_company(company: dict[str, Any]) -> list[dict[str, Any]]:
    research = company.get("competitor_research") or company.get("competition") or {}
    competitors = research.get("competitors") if isinstance(research, dict) else []
    rows: list[dict[str, Any]] = []
    if not isinstance(competitors, list):
        return rows
    for item in competitors[:4]:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "name": item.get("name") or "-",
                "product": item.get("evidence") or item.get("product") or "-",
                "difference": item.get("source_url") or item.get("difference") or "-",
            }
        )
    return rows


def _references_from_state(
    graph_state: dict[str, Any], companies: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(ref: dict[str, Any]) -> None:
        key = str(ref.get("url") or ref.get("title") or "")
        if not key or key in seen:
            return
        seen.add(key)
        references.append(ref)

    for company in companies:
        link = company.get("dart_viewer_link") or (company.get("dart") or {}).get(
            "dart_viewer_link"
        )
        if link:
            add(
                {
                    "type": "report",
                    "author": "DART",
                    "date": "",
                    "title": f"{_company_name(company)} 감사보고서",
                    "url": link,
                }
            )
        screening = (
            company.get("screening")
            if isinstance(company.get("screening"), dict)
            else {}
        )
        for source in screening.get("sources") or []:
            add(
                {
                    "type": "web",
                    "author": "웹 검색",
                    "date": "",
                    "title": f"{_company_name(company)} 투자 라운드 근거",
                    "site": "web",
                    "url": source,
                }
            )
        research = (
            company.get("competitor_research") or company.get("competition") or {}
        )
        for competitor in (
            (research.get("competitors") or []) if isinstance(research, dict) else []
        ):
            url = (competitor or {}).get("source_url")
            if url:
                add(
                    {
                        "type": "web",
                        "author": "Serper",
                        "date": "",
                        "title": (competitor or {}).get("name") or "경쟁사 근거",
                        "site": "web",
                        "url": url,
                    }
                )
        judgement = (
            company.get("judgement")
            if isinstance(company.get("judgement"), dict)
            else {}
        )
        market = judgement.get("external_market_assessment") or {}
        for question in (
            (market.get("questions") or []) if isinstance(market, dict) else []
        ):
            for source in (question or {}).get("evidence") or []:
                url = (source or {}).get("url")
                if url:
                    add(
                        {
                            "type": "web",
                            "author": (source or {}).get("title") or "시장 근거",
                            "date": "",
                            "title": (question or {}).get("question")
                            or "시장성 질문 근거",
                            "site": "web",
                            "url": url,
                        }
                    )
    return references[:20]


def _clip(value: Any, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def build_report_payload(graph_state: dict[str, Any]) -> dict[str, Any]:
    """GraphState(dart/rag/competition/judge)를 ``docs/report.md`` 계약으로 변환합니다."""

    companies = list(graph_state.get("eligible_companies") or [])
    if not companies:
        rejections = list(graph_state.get("judgement_rejections") or [])
        hold_reason = (
            rejections[0].get("reason")
            if rejections
            else "목표 기업 수만큼 투자 적합 기업을 확보하지 못했습니다."
        )
        return {
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
            "references": _references_from_state(graph_state, []),
        }

    featured = _pick_featured(companies) or companies[0]
    featured_judgement = (
        featured.get("judgement") if isinstance(featured.get("judgement"), dict) else {}
    )
    candidates = []
    for company in companies:
        judgement = (
            company.get("judgement")
            if isinstance(company.get("judgement"), dict)
            else {}
        )
        decision = judgement.get("decision") or "보류"
        candidates.append(
            {
                # 4쪽 표 기업명 열(115pt)에 맞게 짧게 자릅니다.
                "name": _clip(_company_name(company), 16),
                "decision": _candidate_decision_label(str(decision)),
                "reason": judgement.get("reason")
                or judgement_reason(company)
                or "근거 없음",
            }
        )

    recommend_count = sum(item.get("decision") == "추천" for item in candidates)
    decision = "recommend" if recommend_count else "hold"
    reasons = list(featured_judgement.get("investment_reasons") or [])
    if not reasons:
        reason = featured_judgement.get("reason") or judgement_reason(featured)
        reasons = [reason] if reason else ["최종 후보 기업의 투자 근거를 요약했습니다."]
    summary_reasons = [_clip(item, 90) for item in reasons[:2]]

    market = featured.get("market") if isinstance(featured.get("market"), dict) else {}
    short_market = _clip(
        market.get("description") or featured.get("description") or "",
        120,
    )
    return {
        "decision": decision,
        "candidates": [
            {
                **item,
                "reason": _clip(item.get("reason"), 80),
            }
            for item in candidates
        ],
        # 보류여도 대표 기업 상세·경쟁사·점수를 보고서에 남깁니다.
        "company": _company_detail(featured),
        "summary_reasons": summary_reasons,
        "top_risk": _clip(
            featured_judgement.get("reason")
            or "재무·경쟁·규제 근거가 추가 검증되면 판단을 재확인합니다.",
            90,
        ),
        "next_check": "DART 재무 수치와 웹 검색 근거가 최신인지 확인합니다.",
        "analysis_headline": _clip(
            market.get("subdomain")
            or featured.get("subdomain")
            or "에너지 인프라 후보 분석",
            60,
        ),
        "market_headline": _clip(
            short_market or "시장 성장과 기업의 실제 기회를 구분합니다", 60
        ),
        "decision_headline": _clip(
            featured_judgement.get("reason") or "적합·보류 근거를 함께 확인합니다",
            60,
        ),
        "market": short_market or "[시장 설명 입력 대기]",
        "demand": _clip(market.get("demand") or "[수요 근거 입력 대기]", 160),
        "differentiation": _clip(
            market.get("differentiation")
            or judgement_reason(featured)
            or "[차별점 입력 대기]",
            160,
        ),
        "competitors": _competitors_from_company(featured),
        "scores": [
            {
                **row,
                "reason": _clip(row.get("reason"), 70),
            }
            for row in _scores_from_company(featured)
        ],
        "risks": "경쟁사 조사·외부시장 점수·VBM 결론이 함께 충족되지 않으면 보류됩니다.",
        "unknowns": "실제 투자금·지분·회수 현금흐름이 없어 ROI는 산출하지 않았습니다.",
        "references": _references_from_state(graph_state, companies),
        "hold_overview": summary_reasons[0] if decision == "hold" else "",
    }


def generate_report(
    state: dict[str, Any], output_path: str | Path, font_path: str | None = None
) -> Path:
    """최대 10개 후보의 평가 결과를 PDF로 표시한다. 판단·출처를 새로 만들지 않는다."""
    candidates = state.get("candidates", [])
    if not 1 <= len(candidates) <= 10:
        raise ValueError("candidates는 1~10개여야 합니다.")
    decision = state.get("decision")
    if decision not in ("recommend", "hold"):
        raise ValueError("decision은 recommend 또는 hold여야 합니다.")
    if len(state.get("summary_reasons", [])) > 2:
        raise ValueError("summary_reasons는 최대 2개입니다.")
    if len(state.get("scores", [])) > 6:
        raise ValueError("scores는 최대 6개입니다.")
    if len(state.get("competitors", [])) > 4:
        raise ValueError("competitors는 최대 4개입니다.")
    if len(state.get("analysis_points", [])) > 4:
        raise ValueError("analysis_points는 최대 4개입니다.")
    company = state.get("company", {})
    if decision == "recommend" and not company.get("name"):
        raise ValueError("추천 시 company.name이 필요합니다.")
    path = Path(output_path)
    p = PageWriter(path, font_path)

    p.start("SUMMARY", "")
    recommended = max(
        sum(item.get("decision") == "추천" for item in candidates),
        1 if decision == "recommend" else 0,
    )
    held = sum(item.get("decision") == "보류" for item in candidates)
    top = p.y
    side_x = R - 128
    left_width = side_x - L - 27
    p.c.setFillColor(MUTED)
    p.c.setFont(BOLD, 9)
    p.c.drawString(L, top - 17, "최종 선정")
    selected_name = company.get("name") if decision == "recommend" else "선정 기업 없음"
    selected = Paragraph(
        escape(_v(selected_name)),
        ParagraphStyle(
            "selected_company",
            fontName=BOLD,
            fontSize=18,
            leading=23,
            textColor=ACCENT,
            wordWrap="CJK",
        ),
    )
    _, selected_height = selected.wrap(left_width, H)
    if selected_height > 46:
        raise ValueError("SUMMARY 선정 기업명이 너무 깁니다.")
    selected.drawOn(p.c, L, top - 28 - selected_height)
    left_y = top - 39 - selected_height

    def summary_note(label: str, value: Any, y: float) -> float:
        p.c.setFillColor(MUTED)
        p.c.setFont(BOLD, 8.5)
        p.c.drawString(L, y - 9, label)
        paragraph = Paragraph(
            escape(_v(value)).replace("\n", "<br/>"),
            ParagraphStyle(
                "summary_note",
                fontName=FONT,
                fontSize=9.2,
                leading=14,
                textColor=INK,
                wordWrap="CJK",
            ),
        )
        _, height = paragraph.wrap(left_width, H)
        if height > 42:
            raise ValueError(f"SUMMARY의 '{label}' 문장이 너무 깁니다.")
        paragraph.drawOn(p.c, L, y - 16 - height)
        return y - 25 - height

    reasons = state.get("summary_reasons") or [
        (
            state.get("hold_overview", "[판단 근거 입력 대기]")
            if decision == "hold"
            else "[선정 근거 입력 대기]"
        )
    ]
    left_y = summary_note(
        "추천 근거" if decision == "recommend" else "보류 이유", reasons[0], left_y
    )
    if len(reasons) > 1:
        left_y = summary_note("추가 판단", reasons[1], left_y)
    left_y = summary_note(
        "핵심 위험", state.get("top_risk", "[핵심 위험 입력 대기]"), left_y
    )
    left_y = summary_note(
        "다음 판단 조건", state.get("next_check", "[추가 확인 사항 입력 대기]"), left_y
    )

    p.c.setStrokeColor(LINE)
    p.c.line(side_x - 15, top - 7, side_x - 15, top - 173)
    for index, (label, value) in enumerate(
        (("검토", len(candidates)), ("추천", recommended), ("보류", held))
    ):
        y = top - 14 - index * 55
        p.c.setFillColor(MUTED)
        p.c.setFont(FONT, 9)
        p.c.drawString(side_x, y - 9, label)
        p.c.setFillColor(ACCENT if label == "추천" else INK)
        p.c.setFont(BOLD, 21)
        p.c.drawRightString(R - 11, y - 25, str(value))
        if index < 2:
            p.c.setStrokeColor(LINE)
            p.c.line(side_x, y - 42, R, y - 42)
    bottom = min(left_y - 4, top - 177)
    if bottom < H / 2:
        raise ValueError("SUMMARY가 반 페이지를 넘었습니다. 문장을 줄이세요.")
    p.c.setStrokeColor(LINE)
    p.c.line(L, bottom, R, bottom)

    p.start(
        "01  종합 분석",
        state.get("analysis_headline") or "기업의 사업·기술·검증 수준을 함께 읽습니다",
    )
    points = state.get("analysis_points", [])
    if points:
        for i, point in enumerate(points, 1):
            p.analysis_block(i, _v(point.get("title")), _v(point.get("body")))
        p.y -= 12
    else:
        p.heading("종합 분석")
        p.text(
            state.get(
                "analysis_detail",
                "[사업·기술·시장·사업화 근거를 연결한 종합 분석 입력 대기]",
            )
        )
    if decision == "recommend":
        p.heading("기업 핵심 정보")
        p.field("기업", company.get("name"))
        p.field("고객 문제", company.get("problem", "[자료 입력 대기]"))
        p.field(
            "사업·기술",
            company.get("technology") or company.get("idea") or "[자료 입력 대기]",
        )
        p.field("팀", company.get("team", "[자료 입력 대기]"))
        p.heading("사업화 근거")
        p.text(company.get("customers_revenue", "[고객·수익 방식 입력 대기]"))
        p.text(company.get("traction", "[실증·계약 입력 대기]"))
    else:
        p.heading("공통 보류 사유")
        p.text(state.get("hold_overview", "[전부 보류 사유 입력 대기]"))
        p.heading("추가 검증 사항")
        p.text(state.get("missing_evidence", "[검증이 필요한 정보 입력 대기]"))

    p.start(
        "02  시장 · 경쟁",
        state.get("market_headline") or "시장 성장과 기업의 실제 기회를 구분합니다",
    )
    p.heading("시장 지표")
    p.market_chart(
        state.get("market_metric_label", ""),
        state.get("market_metric_unit", ""),
        state.get("market_series", []),
        state.get("market_metric_source", ""),
        state.get("market_chart_takeaway", ""),
    )
    p.heading("시장 수치가 뜻하는 것")
    p.text(state.get("market", "[시장 규모·성장 근거 입력 대기]"))
    p.text(state.get("demand", "[수요 요인 입력 대기]"))
    p.heading("경쟁사 비교")
    competitors = state.get("competitors", [])
    if competitors:
        p.table(
            ["기업", "제품·대상", "차이·근거"],
            [
                [x.get("name"), x.get("product"), x.get("difference")]
                for x in competitors
            ],
            [115, 145, R - L - 260],
            35,
        )
    else:
        p.text("[동일 기준으로 비교한 경쟁사 정보 입력 대기]")
    p.heading("차별성 판단")
    p.text(state.get("differentiation", "[검증된 차별점 입력 대기]"))
    if len(candidates) > 5:
        p.heading("주요 리스크 · 한계")
        p.text(state.get("risks", "[사업·기술·규제·경쟁 리스크 입력 대기]"), 9)

    p.start(
        "03  투자 판단 · 리스크",
        state.get("decision_headline")
        or "점수와 근거를 함께 읽고 미확인 사항을 남깁니다",
    )
    p.heading("평가 기준별 판단")
    scores = state.get("scores", [])
    if scores:
        p.table(
            ["평가 항목", "점수", "판단 근거 · 출처"],
            [[x.get("criterion"), x.get("score"), x.get("reason")] for x in scores],
            [125, 55, R - L - 180],
            27,
        )
    else:
        p.text("[평가 항목별 점수·판단 근거 입력 대기]")
    p.heading("평가 후보 요약")
    p.table(
        ["기업", "판단", "주요 이유"],
        [[x.get("name"), x.get("decision"), x.get("reason")] for x in candidates],
        [115, 65, R - L - 180],
        23 if len(candidates) <= 5 else 20,
    )
    if len(candidates) <= 5:
        p.heading("주요 리스크 · 한계")
        p.text(state.get("risks", "[사업·기술·규제·경쟁 리스크 입력 대기]"), 9)
    p.heading("미확인 정보")
    p.text(state.get("unknowns", "[근거가 부족한 항목 입력 대기]"), 9)

    p.start("REFERENCE", "본문의 판단과 수치에 실제로 사용한 자료")
    references = state.get("references", [])
    if references:
        p.text(f"사용 자료 {len(references)}건  |  본문 [번호]와 연결", 9, MUTED, 23)
        for i, item in enumerate(references, 1):
            p.analysis_block(
                i,
                (
                    "기관 보고서"
                    if item.get("type") == "report"
                    else "학술 논문" if item.get("type") == "paper" else "웹페이지"
                ),
                _reference(item),
            )
    else:
        p.text("[실제로 인용한 자료 입력 대기]")
    p.save()
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="투자 평가 결과를 5쪽 PDF로 생성")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--font", default=None, help="한국어 TTF 경로")
    args = parser.parse_args()
    state = json.loads(args.input.read_text(encoding="utf-8"))
    print(generate_report(state, args.output, args.font))


if __name__ == "__main__":
    main()
