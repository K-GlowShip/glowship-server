# 에이전트 실행 추적 (LangSmith 기본 꺼짐 · 내부 로그)

- 대상: `app/modules/delay_analysis/application/agent/tracing.py`, `app/modules/delay_analysis/infrastructure/agent_trace_outbox.py`, 시험 `tests/modules/delay_analysis/test_agent_tracing.py`
- 티켓: GLOW-124. 상위 스토리 AIR-008 "실행 ID로 분석 시작부터 도구 호출, 설명 생성과 종료 상태까지 추적할 수 있다"
- 근거: agentflow 7.1절(호출별 시도 이력은 업무 DB가 아니라 추적에), 8절, 8.1절 LangSmith 경계, serviceflow 5.3절

## 1. 무엇을 남기나
| 사건 `kind` | 언제 | 남기는 것 |
| --- | --- | --- |
| `RUN_START`·`RUN_END`·`RUN_ERROR` | 그래프 실행 전체(부모 없는 체인) | 입력·최종 상태의 키 이름, 안전한 값(`step`·`outcome`·`stopped_at`·`has_at_risk_orders`·`visited_nodes`), 처리 시간 |
| `NODE_START`·`NODE_END`·`NODE_ERROR` | 노드 7개(이름 = `langgraph_node`이고 `graph:step:` 태그) | 단계 번호, 바꾼 상태 키, 처리 시간, 오류 종류(메시지는 마스킹) |
| `ROUTE` | 갈림길 함수(`route_after_prepare`·`route_from_coordinator`·`route_after_explain`) | 다음 노드 이름 |
| `TOOL_ATTEMPT` | 도구 한 번 시도(`call_with_retry(on_attempt=tracer.tool_attempt)`, GLOW-123) | 도구 이름, 시도 번호, 성공·재시도·실패, 오류 코드, 처리 시간, 단계 |
| `EXPLANATION_ATTEMPT` | 설명 생성 시도(엔진 `generate()`의 `attempts`) | 시도 번호, 검증 오류 코드·위치, 모델, 프롬프트 판(`explain_ready_v1@1.0.0`), 토큰 수(NFR-008) |

- **공통 칸:**
  - 사건 ID: `uuid5(실행 ID, 순번)`. 다시 보내도 중복이 생기지 않습니다.
  - 추적 ID: 실행 ID(서비스가 만든 UUID)
  - `analysis_run_id`·`company_id`·순번·시각(UTC)
- **그래프는 고치지 않습니다.** `graph.ainvoke(state, config={"callbacks": [tracer.callback_handler()], "configurable": {...}})`처럼 콜백 하나만 넘깁니다.

```python
tracer = build_run_tracer(
    run_id, company_id, [TraceOutboxSink(base_dir)]
)  # 자동 추적이 켜져 있으면 TraceConfigError
result = await graph.ainvoke(
    state, config={"callbacks": [tracer.callback_handler()], "configurable": {...}}
)
tracer.close()  # 쓰기를 최대 2초 기다림. 늦어도 분석 결과에는 영향 없음
```

## 2. 보내지 않는 것 (마스킹)
| 대상 | 처리 |
| --- | --- |
| 상태 값 | 키 이름과 안전한 키의 값만. 계산 결과·주문 목록 같은 업무 데이터는 남기지 않습니다(원본 업무 데이터 전체 금지) |
| 주문 코드 `A-ORD-YYYYMMDD-NNNN` | 실행마다 새로 만든 키로 HMAC-SHA256 → `ORD#` + 8자리. 같은 실행 안에서는 같은 값이라 묶어 볼 수 있고, 되돌릴 수 없습니다 |
| 이메일 | `[EMAIL]`. 도메인 끝이 글자일 때만 이메일로 봅니다(판 번호 `…@1.0.0`은 남음) |
| 칸 이름이 비밀번호·API 키·인증 토큰·프롬프트 원문·모델 입력 문서·모델 출력 원문 | 값 대신 `[REMOVED]`. 판 번호(`prompt_version`)와 토큰 수(`input_tokens`)는 남깁니다 |
| 긴 값 | 글자 200자, 목록 20개까지 |

## 3. 기본 꺼짐과 내부 로그
- **LangSmith 전송기는 두지 않았습니다.** 업무 데이터 외부 전송 승인(8.1 운영 확인 항목) 전이기 때문입니다. 승인되면 같은 `TraceSink` 모양(`write(event)`)으로 전송기를 더하고, 실패분은 내부 로그에서 다시 보냅니다.
- **자동 추적 차단:** `LANGSMITH_TRACING`·`LANGSMITH_TRACING_V2`·`LANGCHAIN_TRACING_V2`·`LANGCHAIN_TRACING` 중 하나라도 켜져 있으면 LangChain이 그래프 상태를 **마스킹 없이** 보냅니다. 그래서 `build_run_tracer`가 `TraceConfigError`로 멈춥니다.
- **내부 로그:** `<base>/agent_trace_outbox/<company_id>/<YYYY-MM-DD>.jsonl`(UTC 날짜). 사건 하나가 JSON 한 줄이고, 여러 실행이 동시에 써도 줄이 섞이지 않습니다(잠금). `base`는 부르는 쪽(실행기)이 넘깁니다. 설정 값을 둘지는 실행기 연결 때 정합니다(공용 파일 `config.py`를 이번에 바꾸지 않으려는 것).

## 4. 추적 장애 독립성
- 분석 쪽(콜백·훅)은 사건을 만들어 줄에 넣기만 합니다. 쓰기는 실행마다 하나인 쓰기 스레드가 합니다. 그래서 기록이 느려도 분석이 기다리지 않습니다.
- 쓰기 예외·마스킹 예외·콜백 예외는 모두 잡아서 세기만 합니다(`sink_failures`, 경고 로그). 분석 상태·재시도 횟수·실패 요약·결과는 바뀌지 않습니다.
- `close(timeout)`는 쓰기를 기다리되, 시간이 지나면 기다리지 않고 넘어갑니다(남은 쓰기는 뒤에서 계속됨).

## 5. 시험 (16건)
| 시험 | 내용 |
| --- | --- |
| 처음부터 끝까지 | 뼈대 그래프 실행 하나에서 `RUN_START` → 노드·갈림길 → `RUN_END(outcome)`. 노드 순서 = 그래프의 `visited_nodes`, 순번 연속, 모든 사건의 실행·고객사·추적 ID가 같음 |
| 사건 ID | 같은 실행을 두 번 돌려도 사건 ID가 같고, 실행 안에서는 겹치지 않음 |
| STALE·오류 | STALE이면 조정 에이전트 노드에서 멈춘 것이 남음. 상태 조회 오류는 `NODE_ERROR`·`RUN_ERROR`로 남고 메시지 안 주문 코드는 가려짐 |
| 재시도 훅 | `AttemptRecord`와 같은 칸(대역 객체)으로 재시도 → 성공이 `RETRY`·`OK`로 남음 |
| 설명 시도 | 오류 코드·판 번호·토큰 수는 남고, 프롬프트 원문·모델 출력 원문은 없음 |
| 장애 독립성 | 쓰기가 예외를 내도 결과가 추적 없이 돌린 것과 같음. 쓰기가 사건마다 0.2초 걸려도 분석은 기다리지 않음(나중에 다 씀). 마스킹이 깨져도 예외가 나가지 않음 |
| 마스킹 | 주문 코드·이메일 가림, 같은 실행 같은 값, 다른 실행 다른 값. 금지 칸 제거와 수·판 번호 유지. 길이 제한 |
| 기본 꺼짐 | 자동 추적 환경 변수 4개 각각 `TraceConfigError`. 이 저장소의 시험 환경에서 자동 추적이 꺼져 있음 |
| 내부 로그 | 사건마다 한 줄, 경로·날짜, 원래 주문 코드 없음. 실행 4개가 동시에 200줄을 써도 줄이 깨지지 않음 |
| 통합(DB) | 실제 실행 상태 조회기(`AnalysisRunStatusReader`)로 overlay STALE 실행을 돌려 `prepare` → `coordinator`에서 멈춘 것이 내부 로그 마지막 줄(`RUN_END`, `STALE`)까지 남음 |

- **음성 시험(11-05):** 아래 세 가지를 일부러 바꿔 각각 시험이 실패하는 것을 확인했습니다.
  - 주문 코드 마스킹을 끄면 3건 실패
  - 쓰기 예외 세기를 빼면 장애 독립성 시험 실패
  - 쓰기를 분석 쪽에서 바로 하면 느린 기록 시험 실패

## 6. 아직 안 한 것
- **재시도 기록 연결:** `call_with_retry`는 GLOW-123(PR #28)에 있습니다. 이 PR은 `on_attempt`에 넘길 훅(`tracer.tool_attempt`)까지만 만들고, 같은 칸의 대역 객체로 시험했습니다. #28 머지 뒤 실제 `AttemptRecord`로 시험 한 건을 더합니다.
- **그래프에 끼우기:** 실행기(노드 연결 GLOW-129, 자동 시작·저장 GLOW-128)에서 `build_run_tracer`와 `callback_handler()`를 붙이고, 내부 로그 폴더 설정을 정합니다.
- **운영 확인 항목(8.1):** 보존 기간(제안 400일), 외부 전송 승인, 마스킹 범위, 서버 로그의 운영 주체·회전·암호화. 정해지기 전에는 내부 로그만 쓰고 지우지 않습니다.

## 7. 작업 중 실패와 결정
- **칸 이름 필터가 너무 넓었음(11-05):** 처음에는 칸 이름에 `prompt`·`token`이 들어가면 모두 지웠습니다. 시험에서 판 번호 `prompt_version`이 지워졌고, 토큰 수 `input_tokens`도 지워질 상황이었습니다. 프롬프트 원문 칸과 인증 토큰 칸만 지우는 패턴으로 좁혔습니다.
- **이메일 패턴이 판 번호를 가림(11-05):** `explain_ready_v1@1.0.0`이 이메일 모양으로 잡혔습니다. 도메인 끝이 글자(TLD)일 때만 이메일로 봅니다.
- **고치다 넣은 백스페이스 문자(11-05):** 셸에서 정규식을 바꾸다 `\b`가 실제 백스페이스 문자로 들어가, 이메일을 하나도 못 잡았습니다. 시험이 잡았고, 파일 전체에 제어 문자가 없는지 확인했습니다.
- **LangSmith 설정을 넣지 않음:** 켜기·끄기 설정을 `config.py`·`.env.example`에 두려 했지만, 전송기가 승인 전이라 쓸 곳이 없고 두 파일은 다른 PR과 겹치는 공용 파일이라 뺐습니다.
