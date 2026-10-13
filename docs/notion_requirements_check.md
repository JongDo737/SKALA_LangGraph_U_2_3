# 노션 요구사항 점검 (Multi-Agent Orchestration)

상태: ✅ 충족 · 🟡 부분 충족(한계 명시) · ❌ 미충족 · 👤 사용자 작업
근거는 코드 위치 또는 재현 가능한 테스트/실행입니다. (`python -m pytest tests -q` → 62 passed). 노션 원문(Guide A~D, Deliverables)을 다시 읽고 현재 코드·실제 실행 로그와 대조했습니다.

## A. Agent Pattern
| 요구 | 상태 | 근거 |
|---|---|---|
| Supervisor / Orchestrator-Workers 택일 | ✅ | Orchestrator-Workers (`orchestrator/graph.py`) |
| 사전에 계획 수립 | ✅ | `planner` 노드가 워커 실행 전에 서브태스크를 만듦 |
| 계획을 동적 분할해 Workers 에게 병렬 전달 | ✅ | `routing.route_after_plan` → `Send` (서브태스크 수만큼) |
| 최종 결과는 Synthesizer 가 병합 | 🟡 | 노드 이름은 `synthesizer` 가 아니라 기능별로 분리: `merge_results`(집계) → `check_sufficiency` → `investment_decision` → `report_draft`. README 에 Synthesizer 구성으로 명시 |
| Agent-as-Tool, Swarm 미사용 | ✅ | 사용하지 않음 |
| (팀 다이어그램) LLM Orchestrator | 🟡 | 플래너는 **규칙 기반**(같은 State → 같은 계획, 재현성). LLM 플래너는 미구현 |

## B. Mandatory Items (Orchestrator-Workers)
| 요구 | 상태 | 근거 |
|---|---|---|
| 서브태스크 목록을 구조화해 State 에 저장 | ✅ | `OrchestratorState.subtasks: list[Subtask]` |
| 워커는 계획 이후 결정 (동적 fan-out, 고정 fan-out 금지) | ✅ | 최초 20 → 재계획 6 → 2 (테스트 `quality_loop`). 실제 실행: 20 → 6 → 0 (`20261013_172750`), 부족분 보충이 일어난 실행은 20 → 5 → 12 (실행 로그, 폴더는 정리함). `test_fanout_count_follows_plan`, `test_replan_only_reruns_failed_subtasks`. 단 최초 계획은 기업 × 4종(재무 데이터 없으면 제외)이라 동적성은 주로 재계획에서 드러남 |
| 워커 결과 누적 + synthesizer 집계 | ✅ | `results` 리듀서 `merge_results`(회차 기준 최신 우선), `test_results_reducer_newer_round_wins` |
| 일부 worker 실패 시 계속/재시도/제외 중 결정 (Fall-back) | ✅ | 재시도: `record_failure`→`planner`. 소진: `apply_fallback` (재무·시장성 점수는 제외, 나머지는 경고 후 계속). `test_exhaustion_applies_fallback_and_still_reports`. 실제 실행에서도 재무 지표 부족 기업 제외(보충이 일어난 실행 로그). 추가로 `check_quota`: 적합 기업이 목표에 모자라면 DART 검색부터 다시 해 부족분만 보충(최대 1회, `test_topup_*`) |

## C. State Schema (7항목)
| 항목 | 상태 | 근거 |
|---|---|---|
| 제어 vs 페이로드 분리 | ✅ | 본문은 store, State 는 ref/URI·요약. `test_report_body_is_not_in_state` |
| 관측성 위치 | ✅ | 외부 `events.jsonl`(결정+사유+ts). State 에는 Error-aware 최소 필드. `test_control_meta_is_in_state_but_history_is_external` |
| 지속성 비용 | ✅ | 이력성 데이터를 State 에 쌓지 않음 |
| 상관 | 🟡 | `run_id`(=trace_id)가 State·경로·이벤트·LangSmith metadata 에 있음. LangSmith 화면에서 metadata 노출은 직접 확인하지 못함 |
| 재개/복구 | ✅ | SQLite 체크포인터. 노드 중단 후 이어서 실행 `test_resume_from_checkpoint_after_node_crash` |
| 동시 처리 | ✅ | 병렬 워커 쓰기 필드에 리듀서 (`results`, `errors`, `error_count`, `last_error`, `node_status`, `step_count`) |
| 종료 보장 | ✅ | `replan_count ≤ 2`, `step_count < max_steps`. `test_replan_count_never_exceeds_cap[0,1,2]` |
| 설계 근거 정리(README) | ✅ | README `State Schema`, `orchestrator/state.py` 독스트링 |

## D. 보고서 품질 평가
| 요구 | 상태 | 근거 |
|---|---|---|
| 보고서 생성 후 품질 평가 노드 | ✅ | `quality_gate` (report_draft 다음) |
| Groundedness / 중립성 / 편향 통제 | ✅ | `rules.check_report` (인용↔REFERENCE 연결, 우열·추천 표현, 출처 도메인 다양성·단일 도메인 편중·저품질 인용·한계/위험 서술) |
| 미달 시 Loop | ✅ | 워커로 고칠 수 있는 문제는 재계획, 표현·인용 문제는 `report_draft` 보정 후 재평가. 실제 실행 `20261013_172750` 에서 회차 1 중립성 미달 → 회차 2 보정 후 통과(경고 0건). `test_quality_failure_triggers_second_replan`, `test_report_wording_failures_loop_and_get_repaired` |
| 평가 방식 | 🟡 | 규칙 기반(1안에 가깝고 표현·출처 규칙은 내용 일부 반영). LLM Judge(2·3안)는 훅만 있고 미구현 |
| 평가 대상 | 🟡 | 평가는 출처 연결이 명시된 마크다운 초안에 대해 수행하고, PDF 는 같은 데이터로 기존 생성기가 렌더링 |
| 중립성 관련 | 🟡 | 노션 문구는 '특정 **기술** 추천/우열 판정'이지만 우리는 **기업**의 투자 적합 여부를 판정하는 과제라 '적합/부적합' 판정 자체는 평가 목적에 해당함. 다만 기존 PDF 생성기는 (1) 시장성 점수가 가장 높은 기업 1곳을 표지·요약에 '최종 선정'으로 내세우고(`_pick_featured_bundle`), (2) 우리 판정 '적합'을 '추천'으로 바꿔 표기함(실행 `20261013_172750` PDF: '추천' 7회·'최종 선정' 1회). 1위 선정은 기업 간 우열 판정에 가깝고, 우리 품질 평가는 마크다운만 검사하며 마크다운에서는 '추천'을 금지하므로 PDF 와 기준이 어긋남. 팀 결정 필요 |

## E. Deliverables
| 산출물 | 상태 | 비고 |
|---|---|---|
| GitHub 링크 / 기존 작업과 구분되는 브랜치 | 👤 | 요청에 따라 git 미수행 |
| README (샘플 형식, Contributors 개인별 역할, PM·PL 제외) | 🟡 | 샘플 섹션 모두 작성. 핵심 수치(Hit Rate@4 0.981, MRR@10 0.915)는 `scripts/eval_retrieval.py` 로 측정. Contributors 는 기존 역할 그대로라 이번 과제 기준으로 팀이 수정 필요. 핑크 필수 항목 표시는 원문에서 확인 못 함 |
| LangSmith Tracing 이미지 | 👤 | `.env` 설정으로 트레이싱을 켠 실행을 수행했으나 화면 캡처·확인은 사용자 작업. 재계획이 일어난 실행을 캡처 |
| 평가 보고서 PDF (지난 구성, 최대 10장) | ✅ | 5쪽: SUMMARY → 종합 분석 → 시장·경쟁 → 투자 판단·리스크 → REFERENCE (실제 실행 확인) |
| 제출 (zip, Slack) | 👤 | `Agent_{캠퍼스}_{X반}_{이름…}.zip` |

## 평가 항목(배점)별 자체 점검 (노션 '(참고) 평가 항목' 표 기준, 합계 100)
| 항목 (배점) | 노션 채점 포인트 | 현재 상태 | 감점 위험 |
|---|---|---|---|
| 패턴 적용 정합성 (20) | 필수 항목 코드 구현, 라우팅·계획 방식, 통신 제약, 종료 판단, 재작업/fallback | 서브태스크 구조화 저장·동적 fan-out(`Send`)·결과 누적 리듀서·집계 노드·실패 정책(재시도/제외/계속) 구현. 워커끼리 직접 통신 없음(워커 파일 간 import 없음, State 로만 전달). 종료: `replan_count ≤ 2`, `step_count < max_steps`, `recursion_limit` | 플래너가 규칙 기반(팀 다이어그램은 LLM Orchestrator), 'Synthesizer' 노드 이름이 따로 없음(README 에 구성 명시) |
| 동적 동작 실증 (20) | 제출된 실행 트레이스와 코드로 확인, Orchestrator = dynamic fan-out | 코드·테스트로 확인(`test_fanout_count_follows_plan`). 실제 실행에서 20 → 6 → 0, 보충 검색이 일어난 실행은 20 → 5 → 12 | 최초 계획은 기업 × 4종이라 고정처럼 보일 수 있음 → **트레이스 캡처는 재계획·보충이 일어나 Send 가 여러 번 나오는 실행으로**. LangSmith 캡처는 👤 |
| State Schema 설계 (20) | 항목별 설계 근거 명시 + 코드 구현 (제어/페이로드, 관측성, 동시성, 종료 보장 등) | README `State Schema` 7항목 + `orchestrator/state.py` 독스트링 + 테스트. 최종 State 약 16~20KB, 보고서 본문 없음 | 거의 없음 |
| 품질 평가 노드 (15) | 보고서 생성 후 평가 노드가 가이드대로 개발, 미달 시 Loop | `quality_gate`: Groundedness·중립성·편향 통제(1안 규칙 기반), 미달 시 재계획·보정 Loop. 실제 실행에서 미달 → 보정 → 통과 확인 | LLM Judge 없음(1안이라 허용 범위). **PDF 의 '최종 선정 1곳'·'추천' 표기는 평가 대상(마크다운)과 어긋남(위 D 표)** |
| 코드 구조 및 모듈 분리 (5) | 조정 계층과 하위 에이전트 분리, README 디렉토리 구성 일치 | `orchestrator/`(조정) / `agents/`(하위 에이전트), README 디렉토리 구조를 실제 폴더에 맞춤(requirements.txt, .env.example 추가) | 거의 없음 |
| 실행 결과 재현성 (10) | 실행 시 보고서 실제 생성·무한 루프 없이 종료, 제출 trace 와 비교 | 실제 실행 여러 번 정상 종료(SUCCESS), 테스트 62개 통과, `.env.example`·requirements 정비, 벡터 DB 자동 생성. 실행마다 DART 무작위 표본이라 기업·점수는 달라지나 흐름은 같음 | 제출 trace 와 다른 기업이 나오는 점을 README 에 설명 필요 |
| Output - 보고서 (10) | 목적에 맞는 결과, 필수 목차 SUMMARY·REFERENCE | PDF 5쪽(≤10), SUMMARY·REFERENCE 포함, 지난 보고서 구성 | PDF 중립성 표기(위와 동일) |

## 미해결 / 결정 필요
1. 플래너를 LLM 기반으로 바꿀지 (팀 다이어그램은 LLM Orchestrator)
2. 품질 평가에 LLM Judge 를 추가할지 (Hybrid)
3. **PDF 의 '추천'·'최종 선정' 표기**와 중립성 기준 충돌 처리 (위 D 표 ❌, 실제 PDF 에서 확인됨)
4. 경쟁사 출처 확인은 모델 제안 + 검색 방식이며, 기존 웹검색 경쟁사 에이전트(`agents/compitition.py`)는 쓰지 않음 — 팀 합의 필요
