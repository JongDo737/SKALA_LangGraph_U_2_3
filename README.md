# AI Startup Investment Evaluation Agent
본 프로젝트는 에너지 스타트업에 대한 투자 가능성을 자동으로 평가하는 에이전트를 설계하고 구현한 실습 프로젝트입니다.


## Overview
- Objective : AI 스타트업의 {관점1, 관점2, ...} 등을 기준으로 투자 적합성 분석
- Method : AI Agent, Agentic RAG, ...


## Features
- PDF 자료 기반 정보 추출 
- ...


## Tech Stack 
- Framework : LangGraph 
- LLM/Generator : {GPT version}
- LLM/Judge : {GPT version}
- Retrieval : {VectorDB} - {Hit Rate@K}, {MRR} 
- Embedding : {Open-source embedding}


## Agents
- Agent A: ...
- Agent B: ...


## Architecture
(그래프 이미지)


## Directory Structure
├── data/                  # 문서 풀
│   └── companies.json     # 스타트업 기업 원본 데이터
├── agents/                # 평가 기준별 Agent 모듈
│   ├── dart.py             # 기업 공시 검색
│   ├── rag.py              # 시장 조사 및 기업 정보 추출
│   ├── compitition.py      # 경쟁사 조사 및 비교
│   ├── judge.py            # 투자 적합성 판단
│   └── report.py           # 투자 보고서 PDF 생성
├── prompts/               # 프롬프트 템플릿
├── outputs/               # 평가 결과 저장
├── app.py                 # Agent 노드 연결 및 전체 실행
├── .gitignore
└── README.md


## Usage
```bash
python {app.py}
```


## Contributors 
- 김철수 : Prompt Engineering, Agent Design 
- 최영희 : PDF Parsing, Retrieval Agent 
