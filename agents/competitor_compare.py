# 파일 설명: [경쟁사 재무 비교 에이전트] 대상 기업과 DART 재무가 있는 경쟁사의 VBM 지표를 비교합니다.
# 결과는 참고용이며 투자 적합 판정에는 쓰지 않습니다.
# 원본: agents/judge.py 의 compare_competitor_vbm (코드는 그대로 이동)

"""경쟁사 재무 비교 에이전트."""
from __future__ import annotations

from typing import Any

try:
    from agents.financial_vbm import vbm_analysis
except ImportError:  # 단독 실행 시
    from financial_vbm import vbm_analysis

COMPARISON_METRICS = {
    "operating_margin": ("영업이익률", "higher"),
    "net_margin": ("순이익률", "higher"),
    "debt_to_equity": ("부채비율", "lower"),
    "current_ratio": ("유동비율", "higher"),
    "interest_coverage": ("이자보상배율", "higher"),
    "roa": ("ROA", "higher"),
    "roe": ("ROE", "higher"),
    "revenue_cagr": ("매출 CAGR", "higher"),
    "operating_profit_cagr": ("영업이익 CAGR", "higher"),
    "roic_minus_wacc": ("ROIC-WACC", "higher"),
}



def compare_competitor_vbm(
    target_vbm: dict[str, Any], competitor: dict[str, Any] | None, wacc: Any
) -> dict[str, Any]:
    """Compare like-for-like VBM ratios with a DART-verified competitor."""
    dart = competitor.get("dart") if isinstance(competitor, dict) else None
    if not isinstance(dart, dict) or not isinstance(dart.get("financials"), list):
        return {
            "status": "unavailable",
            "reason": "DART 재무제표가 있는 경쟁사 1개를 확보하지 못했습니다.",
            "competitor": None,
            "conclusion": "비교불가",
            "metrics": [],
        }
    competitor_company = {
        "name": dart.get("name") or competitor.get("name"),
        "financials": dart["financials"],
        "wacc": wacc,
    }
    competitor_vbm = vbm_analysis(competitor_company, wacc)
    target_values = {
        item["name"]: item["value"]
        for item in target_vbm.get("metrics", [])
        if item.get("status") == "calculated"
    }
    competitor_values = {
        item["name"]: item["value"]
        for item in competitor_vbm.get("metrics", [])
        if item.get("status") == "calculated"
    }
    rows: list[dict[str, Any]] = []
    wins = losses = 0
    for metric_id, (label, direction) in COMPARISON_METRICS.items():
        target_value, competitor_value = target_values.get(
            metric_id
        ), competitor_values.get(metric_id)
        if target_value is None or competitor_value is None:
            continue
        # Ratio differences below 1%p are treated as practically similar.
        if abs(target_value - competitor_value) < 0.01:
            result = "유사"
        elif (target_value > competitor_value) == (direction == "higher"):
            result = "우위"
            wins += 1
        else:
            result = "열위"
            losses += 1
        rows.append(
            {
                "metric": metric_id,
                "label": label,
                "target_value": target_value,
                "competitor_value": competitor_value,
                "direction": direction,
                "result": result,
            }
        )
    if len(rows) < 3:
        conclusion, status, reason = (
            "비교불가",
            "unavailable",
            "공통으로 계산 가능한 VBM 지표가 3개 미만입니다.",
        )
    elif wins >= losses + 2:
        conclusion, status, reason = (
            "우위",
            "complete",
            "공통 VBM 지표에서 대상 기업의 우위 항목이 더 많습니다.",
        )
    elif losses >= wins + 2:
        conclusion, status, reason = (
            "열위",
            "complete",
            "공통 VBM 지표에서 대상 기업의 열위 항목이 더 많습니다.",
        )
    else:
        conclusion, status, reason = (
            "유사",
            "complete",
            "공통 VBM 지표에서 뚜렷한 우위 또는 열위가 확인되지 않습니다.",
        )
    return {
        "status": status,
        "reason": reason,
        "competitor": {
            "name": competitor_company["name"],
            "corp_code": dart.get("corp_code"),
            "dart_viewer_link": dart.get("dart_viewer_link"),
            "statement_years": competitor_vbm.get("statement_years", []),
            "financial_source": dart.get("financial_source"),
        },
        "competitor_vbm_assessment": competitor_vbm,
        "conclusion": conclusion,
        "wins": wins,
        "losses": losses,
        "metrics": rows,
    }




def run_competitor_compare_agent(
    target_vbm: dict[str, Any], competitor: dict[str, Any] | None, wacc: Any = None
) -> dict[str, Any]:
    """[에이전트 진입점] 대상 기업 VBM과 경쟁사 1곳의 VBM을 비교합니다."""

    return compare_competitor_vbm(target_vbm, competitor, wacc)
