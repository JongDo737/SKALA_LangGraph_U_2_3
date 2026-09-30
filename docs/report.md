# 투자 보고서 입력 계약 (초안)

`agents/report.py`는 앞 에이전트의 판단을 새로 계산하지 않고 PDF에 배치합니다. 교수님 실습 설명의 보고서 조건은 첫 장 SUMMARY에 전체 판단의 결론을 문서 소개 없이 A4 반 페이지 이내로 요약하고, 모든 후보를 보류하면 그 이유를 적으며, 마지막 장 REFERENCE에 실제 사용한 자료만 넣고, 전체를 5장 이내로 구성하는 것입니다. 현재 SUMMARY는 선정 기업, 추천 근거 또는 보류 이유, 추가 판단, 핵심 위험, 다음 판단 조건과 후보 수를 표시합니다.

추천 기업이 여러 곳이면 `judgement.external_market_score`가 가장 높은 추천 기업 한 곳을 SUMMARY의 최종 선정 및 상세 분석 대상으로 표시합니다. 동점이면 입력 순서를 따릅니다. 모두 보류면 최고 점수 후보를 보류 사유 설명의 대표 사례로 사용하며 최종 선정 기업은 표시하지 않습니다.

LangGraph에서는 judge 이후 `prepare_report`(`agents/report_prep.py`)가 필수 필드만 남기고 LLM으로 서술·출처를 보강한 뒤 `docs/report.md` 계약(`report_payload`)을 만듭니다. `report_node`는 이 페이로드로 PDF를 생성합니다. (`report_payload`가 없으면 `build_report_payload`로 폴백합니다.)

`prepare_report`가 쓰는 원천·규칙:

- 포함: `company_name`, `corp_code`, `market_context.inferred.description`, `market_context.summary`(시장 규모), `ceo`, `financial_summary`(한글만, normalized 제외), `screening.stage_source`, `dart_viewer_link`/`report_nm`/`rcept_dt`, `market_context.sources`, `judgement`/`evaluation`
- 제외: `bizr_no`, `address`, 빈 `homepage_url`, `pdf_path`, `audit_reports_count`, 중복 `latest_report`
- 보강: AI 데이터센터 전력 인프라 관련성, 시장·기술·규제·경쟁 리스크, 창업자 기술 역량, 시장 자료 발행기관·연도·URL(파일명·메타 기반, 불완전하면 명시), 투자 판단·점수·이유·출처

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
- `company`: `problem`, `idea`, `technology`, `customers_revenue`, `team`, `traction`
- `hold_overview`, `missing_evidence`: 모두 보류일 때의 공통 설명
- `market`, `demand`, `differentiation`
- `market_metric_label`, `market_metric_unit`, `market_metric_source`: 시장 그래프의 지표명·단위·본문 출처 번호
- `market_chart_takeaway`: 시장 그래프 아래에 놓을 한 줄 해석. 시장 수치를 기업 매출 전망으로 혼동하지 않도록 범위를 명시
- `market_series`(최대 5개): `period`, `value`(0 이상 숫자), `kind`(`actual` 또는 `forecast`). 원자료의 실적·전망 구분을 그대로 사용
- `competitors`(최대 4개): `name`, `product`, `difference`
- `scores`(최대 6개): `criterion`, `score`, `reason`. 평가 항목별 판단 표는 4쪽에 표시합니다
- `risks`: 시장·기술·규제·경쟁 리스크와 한계를 2~3문장으로 작성 (최대 420자)
- `references`: 실제 본문에서 인용한 자료만 입력. 각 항목에 `type`(`report`, `paper`, `web`), `author`, `date`, `title`와 유형별 `url`, `site`, `journal`, `volume_pages`를 입력

본문 길이(표가 아닌 서술):
- `company.idea` 420자, `company.team` 360자
- `market` 420자, `demand` 360자, `differentiation` 320자
- `risks` 420자, `analysis_detail` 360자
- SUMMARY(`summary_reasons`, `top_risk`, `next_check`)는 A4 반 페이지 높이 제한을 우선합니다.

표 한 칸은 PDF에서 **한 줄·고정 폭**입니다. `prepare_report`는 `stringWidth` 기준으로 셀을 잘라 `competitors`/`scores`/`candidates`가 넘치지 않게 합니다. 길거나 5쪽을 넘는 입력은 조용히 생략하지 않고 오류로 알립니다. `unknowns`(미확인 정보)는 보고서에 넣지 않습니다.

저장소에는 가상 기업·시장 수치를 담은 입력 파일을 포함하지 않습니다. 앞 에이전트의 실제 평가 결과를 JSON으로 전달하고, 실제로 인용한 자료만 `references`에 넣어 PDF를 생성합니다. 최종 제출 파일 이름은 팀 정보 확정 후 실습 지침의 `RAG-Output_{캠퍼스}-{X반}_{이름...}.pdf`로 지정합니다.
