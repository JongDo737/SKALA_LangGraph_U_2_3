# Orchestrator-Workers 계층

기존 순차 파이프라인(`app_legacy.py`)을 **Orchestrator-Workers** 로 바꾼 구현입니다.
`agents/` 는 기능별 에이전트, `orchestrator/` 는 조정 계층입니다.

## 실행

```bash
source .venv/bin/activate
python app.py --company-count 3                          # 실제 DART·검색·LLM (.env 필요, LangSmith 트레이싱은 .env 설정 따름)
python app.py --company-count 3 --no-trace               # 트레이싱 끔
python app.py --max-replans 2                            # 재계획(재작업) 루프 상한 (기본 2)
python -m pytest tests -q                                # 테스트 (가짜 응답기 사용)
```
테스트 시나리오(`tests/fake_services.py`): `fixture`(자연 발생 재계획) · `profile_retry`(프로필 추가 보완) · `quality_loop`(품질 평가로 2차 재계획) · `exhaust`(상한 소진 fallback)

## 흐름 (팀 다이어그램)

```
START → dart_lookup → startup_screen → profile_prep(웹검색) → planner
planner ─ Send(동적 fan-out) → worker_financial | worker_market_rag | worker_market_score | worker_competitor
        → merge_results → validate_results → check_sufficiency
   실패(검증·근거 부족) / 품질 미달 → record_failure → [예산 남음] planner 재계획 (실패한 것만, 프로필 약하면 profile_prep 먼저)
                                                      [소진]      apply_fallback (제외·경고) → 진행
   통과 → competitor_compare → investment_decision → check_quota → report_draft → quality_gate → finalize_report → END
                                                      └ 적합 < 목표 (최대 1회) → dart_lookup (부족분만 보충)
```
- **루프 상한 2회**: `settings.max_replans`. 검증·근거 충분성·품질 평가 어느 단계의 실패든 같은 예산을 씁니다.
- **부족분 보충 1회**: `settings.max_topups`. 적합 기업이 목표(5곳)에 못 미치면 `check_quota` 가 DART 검색부터 다시 요청합니다. 이미 평가한 기업은 그대로 두고(다시 가져오지도, 다시 돌리지도 않음) 부족한 수만큼만 새로 평가합니다. 재계획 예산과 별개입니다.
- 워커로 고칠 수 없는 실패(보고서 문구 등)는 예산을 쓰지 않고 바로 fallback 으로 갑니다.
- 소진 시 fallback: 재무·시장성 점수를 못 얻은 기업은 **제외**, 프로필·시장 자료·경쟁사는 **경고로 표기하고 계속**, 품질 미달은 경고를 남기고 보고서 확정.

## 노드 = 기능 하나 (`orchestrator/nodes/`)

| 노드 | 기능 | 사용하는 agents/ 모듈 |
|---|---|---|
| dart_lookup | 후보 수집 | `agents/dart.py` |
| startup_screen | 스타트업·투자 단계 필터 | `agents/startup_screen.py` |
| profile_prep | 웹검색으로 사업 설명·분야·출처 확정 | (Serper + LLM, 검색 결과만 근거) |
| planner | 서브태스크 계획·재계획 (규칙 기반) | — |
| worker_financial | VBM 재무 계산 | `agents/financial_vbm.py` |
| worker_market_rag | 시장 규모·성장성 근거 | `agents/market_agent.py`, `rag.py` |
| worker_market_score | 시장성 질문 10개 채점 | `agents/market_scoring.py` |
| worker_competitor | 경쟁사 조사 | `agents/compitition.py` |
| merge_results | 결과 도착 집계 (병합은 리듀서) | — |
| validate_results | 결과 존재·형식 검증 | — |
| check_sufficiency | 필수 근거 충분성 | — |
| competitor_compare | 경쟁사 재무 비교(참고) | `agents/competitor_compare.py` |
| investment_decision | 적합/부적합 판정 | `agents/investment_decision.py` |
| check_quota | 적합 수가 목표에 모자라면 부족분 보충 검색 요청 | — |
| report_draft | SUMMARY·기업별·REFERENCE 초안 | — |
| quality_gate | Groundedness·중립성·편향 통제 | — |
| record_failure / apply_fallback | 실패 기록 / 소진 후 제외·경고 | — |
| finalize_report / save_unresolved | 보고서 확정 / 미해결 저장 | — |

`agents/judge.py` 는 기능별 에이전트(`financial_vbm`, `market_scoring`, `competitor_compare`, `investment_decision`)로 분리했고,
기존 `import agents.judge` 호환용 진입점만 남겼습니다. 분리 전후 결과가 같음을 `tests/test_judge_split_equivalence.py` 가 확인합니다.

## State (`orchestrator/state.py`)

Layered State: `OrchestratorState`(상위) + `WorkerState`(Send 로 전달되는 워커 단위, `worker_id = 에이전트이름_…_timestamp`).

| 항목 | 설계 |
|---|---|
| 제어 vs 페이로드 | State 는 계획·상태·요약·ref/URI. 프로필·워커 결과·보고서 본문은 `outputs/runs/{run_id}/` 저장소 |
| 관측성 위치 | 결정·계획·실패·품질 이력은 State 가 아니라 외부 `events.jsonl` (`observability.py`) |
| 지속성 비용 | 체크포인트 State 는 요약 크기. 이력은 State 에 쌓지 않음 |
| 상관 | `run_id`(= trace_id) 를 State·저장 경로·이벤트 로그·LangSmith metadata 에 공통 사용 |
| 재개/복구 | `status`·`step_count`·`node_status`·`last_error`·`replan_count`·결과 ref. `--checkpoint sqlite`, `--resume RUN_ID` |
| 동시 처리 | `results`·`errors`·`error_count`·`last_error`·`node_status`·`step_count` 에 리듀서 |
| 종료 보장 | `replan_count ≤ max_replans(2)`, `step_count < max_steps` 확인, `recursion_limit` 은 최후 안전장치 |

Error-aware(SKALA): `status`(RUNNING/SUCCESS/FAILED), `step_count`/`max_steps`, `error_count`, `last_error {node,type,message,ts}`,
`errors [{agent,error,ts}]`, `results[...]{agent,worker_id,status,ts,ref}`.

## 알려진 한계
- 플래너는 **규칙 기반**입니다(같은 State → 같은 계획, 재현성). 팀 다이어그램의 "LLM Orchestrator"와 다릅니다.
- 품질 평가는 규칙 기반이며 LLM 심사 훅(`services.quality_review`)만 열어 두었고 구현하지 않았습니다.
- 경쟁사: 1차는 모델 제안, 재작업에서 검색으로 해당 회사를 다루는 외부 출처를 확인합니다. 기존 웹검색 경로(`agents/compitition.py`)는 페이지 제목에서 이름을 뽑아 '보도자료' 같은 분류명이 잡혀 쓰지 않습니다. 검색으로 확인하지 못하면 근거 없음으로 남고 보고서에 한계로 표기됩니다.
- 보고서 PDF 는 단순 렌더러입니다. 기존 `agents/report.py` 의 PDF 는 이 흐름에 연결하지 않았습니다.
