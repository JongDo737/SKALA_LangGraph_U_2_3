"""judge.py 분리 전후 결과가 같은지 확인합니다.

원본 judge.py는 tests/fixtures/judge_original.py 에 보관한 사본이며, 저장된 실제 실행 결과
(tests/fixtures/final_companies_sample.json)를 같은 입력으로 두 구현에 넣어 비교합니다.
네트워크·API 키가 필요 없습니다. (시장성 점수는 저장된 질문별 점수를 그대로 입력)
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.compitition import EmptySearchClient  # noqa: E402


def _load_original():
    spec = importlib.util.spec_from_file_location(
        "judge_original", FIXTURES / "judge_original.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _items():
    data = json.loads((FIXTURES / "final_companies_sample.json").read_text(encoding="utf-8"))
    for company in data["eligible_companies"]:
        previous = company["judgement"]["external_market_assessment"]
        supplied = {
            q["id"]: {
                "score": q["score"],
                "reason": q["score_reason"],
                "source_indices": q["source_indices"],
            }
            for q in previous["questions"]
        }
        research = company.get("competitor_research") or {}
        yield {
            "company": {
                "name": company["name"],
                "description": company["description"],
                "country": company["country"],
                "financials": company["financials"],
                "wacc": 0.1,
                "external_market_scores": supplied,
            },
            "subdomain": company["subdomain"],
            "requested_competitor_count": research.get("requested_competitor_count", 1),
            "found_competitor_count": research.get("found_competitor_count", 1),
            "competitors": research.get("competitors", []),
        }


@pytest.mark.parametrize("item", list(_items()), ids=lambda i: i["company"]["name"])
def test_judge_company_same_as_original(item):
    import agents.judge as split

    original = _load_original()
    a = original.judge_company(copy.deepcopy(item), EmptySearchClient(), 0.1, None)
    b = split.judge_company(copy.deepcopy(item), EmptySearchClient(), 0.1, None)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(
        b, sort_keys=True, default=str
    )


@pytest.mark.parametrize("item", list(_items()), ids=lambda i: i["company"]["name"])
def test_financial_agent_matches_original_vbm(item):
    from agents.financial_vbm import run_financial_agent

    original = _load_original()
    result = run_financial_agent(item["company"], 0.1)
    expected_vbm = original.vbm_analysis(item["company"], 0.1)
    assert result["vbm"] == expected_vbm
    assert result["acceptable"] == original.conditional_vbm_acceptable(expected_vbm)
