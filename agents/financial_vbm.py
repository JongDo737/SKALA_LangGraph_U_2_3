# 파일 설명: [재무 계산 에이전트] 대상 기업의 DART 재무로 VBM 지표를 계산하고
# 재무 투자 기준 통과 여부를 판정합니다. (LLM·검색 없음, 순수 계산)
# 원본: agents/judge.py 의 vbm_analysis / conditional_vbm_acceptable (코드는 그대로 이동)

"""재무 계산 에이전트.

오케스트레이터는 ``run_financial_agent`` 하나만 호출하면 됩니다.
"""
from __future__ import annotations

import math
from typing import Any

# DART-style Korean keys are primary; normalized English keys are also allowed.
KEYS = {
    "assets": ("자산총계", "total_assets"),
    "liabilities": ("부채총계", "total_liabilities"),
    "equity": ("자본총계", "total_equity"),
    "revenue": ("매출액", "revenue"),
    "operating_profit": ("영업이익", "operating_income"),
    "net_income": ("당기순이익", "net_income"),
    "tax_expense": ("법인세비용", "income_tax_expense"),
    "cash": ("현금및현금성자산", "cash_and_cash_equivalents"),
    "debt": ("이자부부채", "이자부채", "interest_bearing_debt"),
    "interest_expense": ("이자비용", "interest_expense"),
    "operating_cf": ("영업활동현금흐름", "operating_cash_flow"),
    "capex": ("CAPEX", "설비투자", "유형자산의취득", "capital_expenditure"),
    "current_assets": ("유동자산", "current_assets"),
    "current_liabilities": ("유동부채", "current_liabilities"),
}



def number(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(raw) else None
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text.lower() in {"null", "none", "-", "n/a"}:
            return None
        # 회계 음수 표기: (1,234) → -1234
        negative = False
        if text.startswith("(") and text.endswith(")"):
            negative = True
            text = text[1:-1]
        text = text.replace(",", "").replace(" ", "")
        try:
            value = float(text)
            if negative:
                value = -abs(value)
            return value if math.isfinite(value) else None
        except ValueError:
            pass
    return None


def field(row: dict[str, Any], name: str) -> float | None:
    return next(
        (result for key in KEYS[name] if (result := number(row.get(key))) is not None),
        None,
    )


def divide(top: float | None, bottom: float | None) -> float | None:
    return None if top is None or bottom in (None, 0) else top / bottom


def metric(
    name: str, value: float | None, formula: str, missing: str, unit: str = "ratio"
) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "unit": unit if value is not None else None,
        "formula": formula,
        "status": "calculated" if value is not None else "unavailable",
        "unavailable_reason": None if value is not None else missing,
    }


def records(company: dict[str, Any]) -> list[dict[str, Any]]:
    rows = company.get("financials", company.get("financial_statements", [company]))
    if not isinstance(rows, list):
        return []
    valid = [row for row in rows if isinstance(row, dict)]
    return sorted(
        valid,
        key=lambda row: number(row.get("year") or row.get("사업연도")) or float("inf"),
    )


def cagr(first: float | None, last: float | None, years: int) -> float | None:
    return (
        None
        if first is None or last is None or first <= 0 or last < 0 or years <= 0
        else (last / first) ** (1 / years) - 1
    )


def vbm_analysis(company: dict[str, Any], default_wacc: Any) -> dict[str, Any]:
    statements = records(company)
    if not statements:
        return {
            "status": "unavailable",
            "reason": "financials must be a list of statement objects",
            "metrics": [],
        }
    current, prior = statements[-1], statements[-2] if len(statements) > 1 else None
    revenue, op, net = (
        field(current, key) for key in ("revenue", "operating_profit", "net_income")
    )
    assets, liabilities, equity = (
        field(current, key) for key in ("assets", "liabilities", "equity")
    )
    items = [
        metric(
            "operating_margin",
            divide(op, revenue),
            "operating_profit / revenue",
            "매출액과 영업이익이 필요합니다.",
        ),
        metric(
            "net_margin",
            divide(net, revenue),
            "net_income / revenue",
            "매출액과 당기순이익이 필요합니다.",
        ),
        metric(
            "debt_to_equity",
            divide(liabilities, equity),
            "total_liabilities / total_equity",
            "부채총계와 자본총계가 필요합니다.",
        ),
        metric(
            "current_ratio",
            divide(
                field(current, "current_assets"), field(current, "current_liabilities")
            ),
            "current_assets / current_liabilities",
            "유동자산과 유동부채가 필요합니다.",
        ),
        metric(
            "interest_coverage",
            divide(op, field(current, "interest_expense")),
            "operating_profit / interest_expense",
            "영업이익과 이자비용이 필요합니다.",
        ),
    ]
    if prior:
        old_assets, old_equity = field(prior, "assets"), field(prior, "equity")
        avg_assets = (
            (assets + old_assets) / 2
            if assets is not None and old_assets is not None
            else None
        )
        avg_equity = (
            (equity + old_equity) / 2
            if equity is not None and old_equity is not None
            else None
        )
        items += [
            metric(
                "roa",
                divide(net, avg_assets),
                "net_income / average_total_assets",
                "당기순이익과 2개년 자산총계가 필요합니다.",
            ),
            metric(
                "roe",
                divide(net, avg_equity),
                "net_income / average_total_equity",
                "당기순이익과 2개년 자본총계가 필요합니다.",
            ),
            metric(
                "revenue_cagr",
                cagr(field(statements[0], "revenue"), revenue, len(statements) - 1),
                "(latest_revenue / first_revenue)^(1/years)-1",
                "2개년 이상 양(+)의 매출액이 필요합니다.",
            ),
            metric(
                "operating_profit_cagr",
                cagr(field(statements[0], "operating_profit"), op, len(statements) - 1),
                "(latest_operating_profit / first_operating_profit)^(1/years)-1",
                "2개년 이상 양(+)의 영업이익이 필요합니다.",
            ),
        ]
    else:
        items += [
            metric(
                "roa",
                None,
                "net_income / average_total_assets",
                "전년도 자산총계가 필요합니다.",
            ),
            metric(
                "roe",
                None,
                "net_income / average_total_equity",
                "전년도 자본총계가 필요합니다.",
            ),
            metric("revenue_cagr", None, "CAGR", "2개년 이상 매출액이 필요합니다."),
            metric(
                "operating_profit_cagr",
                None,
                "CAGR",
                "2개년 이상 영업이익이 필요합니다.",
            ),
        ]
    tax, debt, cash = (
        field(current, "tax_expense"),
        field(current, "debt"),
        field(current, "cash"),
    )
    pretax = net + tax if net is not None and tax is not None else None
    tax_rate = divide(tax, pretax)
    invested_capital = (
        debt + equity - cash if None not in (debt, equity, cash) else None
    )
    nopat = op * (1 - tax_rate) if op is not None and tax_rate is not None else None
    roic = divide(nopat, invested_capital)
    items.append(
        metric(
            "roic",
            roic,
            "NOPAT / (interest_bearing_debt + equity - cash)",
            "법인세비용, 이자부채, 현금, 자본 및 영업이익이 필요합니다.",
        )
    )
    wacc = number(company.get("wacc", default_wacc))
    items.append(
        metric(
            "roic_minus_wacc",
            roic - wacc if roic is not None and wacc is not None else None,
            "ROIC - WACC",
            "계산 가능한 ROIC와 WACC 가정이 필요합니다.",
        )
    )
    ocf, capex = field(current, "operating_cf"), field(current, "capex")
    items.append(
        metric(
            "free_cash_flow",
            ocf - abs(capex) if ocf is not None and capex is not None else None,
            "operating_cash_flow - abs(CAPEX)",
            "영업활동현금흐름과 CAPEX가 필요합니다.",
            "currency",
        )
    )
    values = {
        item["name"]: item["value"] for item in items if item["status"] == "calculated"
    }

    # 공시에 자주 있는 지표(ROE/ROA/유동비율/이자보상)까지 양호 신호로 인정합니다.
    # 양호 3개 이상이면 긍정. ROIC≤WACC만으로 즉시 주의 처리하지 않습니다.
    positive_flags = {
        "operating_margin": values.get("operating_margin") is not None
        and values["operating_margin"] > 0,
        "net_margin": values.get("net_margin") is not None and values["net_margin"] > 0,
        "free_cash_flow": values.get("free_cash_flow") is not None
        and values["free_cash_flow"] > 0,
        "roic_minus_wacc": values.get("roic_minus_wacc") is not None
        and values["roic_minus_wacc"] > 0,
        "revenue_cagr": values.get("revenue_cagr") is not None
        and values["revenue_cagr"] > 0,
        "roe": values.get("roe") is not None and values["roe"] > 0,
        "roa": values.get("roa") is not None and values["roa"] > 0,
        "interest_coverage": values.get("interest_coverage") is not None
        and values["interest_coverage"] > 1,
        "current_ratio": values.get("current_ratio") is not None
        and values["current_ratio"] >= 1,
        "debt_to_equity": values.get("debt_to_equity") is not None
        and 0 < values["debt_to_equity"] <= 5,
        "operating_profit_cagr": values.get("operating_profit_cagr") is not None
        and values["operating_profit_cagr"] > 0,
    }
    positive = sum(positive_flags.values())
    positive_names = [name for name, ok in positive_flags.items() if ok]
    coverage = len(values) / len(items)
    if coverage < 0.4:
        conclusion, reason = "보류", "VBM 핵심 지표의 입력 데이터가 부족합니다."
    elif positive >= 3:
        conclusion, reason = (
            "긍정",
            f"계산 가능한 수익성·안정성·성장성 지표 중 {positive}개가 양호합니다.",
        )
    elif values.get("roic_minus_wacc") is not None and values["roic_minus_wacc"] <= 0:
        conclusion, reason = (
            "주의",
            "양호 지표가 부족하고 ROIC가 WACC를 초과하지 않아 가치 창출 근거가 약합니다.",
        )
    else:
        conclusion, reason = (
            "주의",
            "계산 가능한 VBM 지표에서 가치 창출 근거가 충분하지 않습니다.",
        )
    return {
        "status": "available",
        "statement_years": [row.get("year", row.get("사업연도")) for row in statements],
        "metrics": items,
        "data_coverage": coverage,
        "wacc": wacc,
        "positive_signal_count": positive,
        "positive_signals": positive_names,
        "conclusion": conclusion,
        "conclusion_reason": reason,
    }



def conditional_vbm_acceptable(vbm: dict[str, Any]) -> bool:
    """VBM 적합 여부를 판정합니다.

    - 긍정: 통과
    - 주의: 영업이익률 또는 순이익률이 양수면 통과
      (ROIC≤WACC만으로 탈락시키지 않음 — 완화 기준)
    - 보류/그 외: 탈락
    """

    if vbm.get("conclusion") == "긍정":
        return True
    if vbm.get("conclusion") != "주의":
        return False
    values = {
        entry.get("name"): entry.get("value")
        for entry in vbm.get("metrics", [])
        if entry.get("status") == "calculated"
    }
    operating_margin = values.get("operating_margin")
    net_margin = values.get("net_margin")
    if operating_margin is not None and operating_margin > 0:
        return True
    if net_margin is not None and net_margin > 0:
        return True
    # 마진을 계산하지 못했더라도 ROIC>WACC이면 통과
    spread = values.get("roic_minus_wacc")
    return spread is not None and spread > 0




def run_financial_agent(company: dict[str, Any], wacc: Any = None) -> dict[str, Any]:
    """[에이전트 진입점] 기업 1곳의 VBM 분석과 재무 기준 통과 여부를 반환합니다."""

    vbm = vbm_analysis(company, wacc)
    return {"vbm": vbm, "acceptable": conditional_vbm_acceptable(vbm)}
