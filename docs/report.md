# 투자 보고서 입력 계약 (초안)

`agents/report.py`는 앞 에이전트의 판단을 새로 계산하지 않고 PDF에 배치합니다. 교수님 실습 설명의 보고서 조건은 첫 장 SUMMARY에 전체 판단의 결론을 문서 소개 없이 A4 반 페이지 이내로 요약하고, 모든 후보를 보류하면 그 이유를 적으며, 마지막 장 REFERENCE에 실제 사용한 자료만 넣고, 전체를 5장 이내로 구성하는 것입니다. 현재 SUMMARY는 선정 기업, 추천 근거 또는 보류 이유, 추가 판단, 핵심 위험, 다음 판단 조건과 후보 수를 표시합니다. 팀의 LangGraph State가 확정되면 `generate_report`에 전달하는 필드 이름을 맞춥니다.

```bash
python -m pip install -r requirements-report.txt
python -m agents.report --input /path/to/report_state.json --output outputs/report.pdf
```

프로젝트에 포함된 Pretendard TTF가 PDF에 자동 포함됩니다. 다른 한국어 TTF를 시험할 때만 `--font /path/to/font.ttf`를 지정합니다.

## 필수 필드

| 필드 | 내용 |
|---|---|
| `decision` | `recommend` 또는 `hold` |
| `candidates` | 1~10개. 각 항목은 `name`, `decision`, `reason` |
| `company.name` | `recommend`일 때 필수. 추천 기업의 상세 정보는 `company`에 입력 |

## 선택 필드

- `summary_reasons`(최대 2개), `top_risk`, `next_check`, `analysis_detail`, `analysis_points`(최대 4개, 각 `title`, `body`)
- `analysis_headline`, `market_headline`, `decision_headline`: 각 본문 페이지 상단에 놓을 한두 줄짜리 결론. 자료에서 확인된 판단만 적고, 없으면 중립적인 기본 문장을 표시
- `company`: `problem`, `idea`, `technology`, `customers_revenue`, `team`, `traction`
- `hold_overview`, `missing_evidence`: 모두 보류일 때의 공통 설명
- `market`, `demand`, `differentiation`
- `market_metric_label`, `market_metric_unit`, `market_metric_source`: 시장 그래프의 지표명·단위·본문 출처 번호
- `market_chart_takeaway`: 시장 그래프 아래에 놓을 한 줄 해석. 시장 수치를 기업 매출 전망으로 혼동하지 않도록 범위를 명시
- `market_series`(최대 5개): `period`, `value`(0 이상 숫자), `kind`(`actual` 또는 `forecast`). 원자료의 실적·전망 구분을 그대로 사용
- `competitors`(최대 4개): `name`, `product`, `difference`
- `scores`(최대 6개): `criterion`, `score`, `reason`. 평가 항목별 판단 표는 4쪽에 표시합니다
- `risks`, `unknowns`
- `references`: 실제 본문에서 인용한 자료만 입력. 각 항목에 `type`(`report`, `paper`, `web`), `author`, `date`, `title`와 유형별 `url`, `site`, `journal`, `volume_pages`를 입력

본문의 주요 주장에는 `[1]`처럼 출처 번호를 직접 붙이고, `references` 배열을 같은 순서로 전달합니다. 출처를 확인하지 못한 주장이나 수치를 사실처럼 쓰지 않습니다. 표 한 칸에 들어갈 판단 이유는 짧은 요약문으로 넘겨주세요. 길거나 5쪽을 넘는 입력은 조용히 생략하지 않고 오류로 알립니다.

저장소에는 가상 기업·시장 수치를 담은 입력 파일을 포함하지 않습니다. 앞 에이전트의 실제 평가 결과를 JSON으로 전달하고, 실제로 인용한 자료만 `references`에 넣어 PDF를 생성합니다. 최종 제출 파일 이름은 팀 정보 확정 후 실습 지침의 `RAG-Output_{캠퍼스}-{X반}_{이름...}.pdf`로 지정합니다.
