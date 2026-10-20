# 회귀 테스트용 쿼리 목록 — 시스템명 하드코딩 수정 검증

| # | 쿼리 | 테스트 목적 |
|---|---|---|
| 1 | payments-service 결제 응답이 느려지고 CPU가 치솟았어요, 원인 확인해주세요 | 기존엔 auth-service로 조용히 되돌아갔던 4개 시스템 중 하나 — KNOWN_SYSTEMS 인식 확인 (cpu-spike runbook) |
| 2 | cache-service 메모리 사용률이 계속 올라가고 있어요 | 미인식 시스템 확인 2 (memory-leak runbook) |
| 3 | log-service 디스크 공간이 부족하다는 알림이 왔어요 | 미인식 시스템 확인 3 (disk-full runbook) |
| 4 | gateway-service에서 payments-service로 가는 요청이 타임아웃 나고 있어요 | 두 시스템 동시 언급 — system_name은 먼저 등장한 gateway-service 하나만 뽑히는지, 그런데도 research task는 두 시스템 다 조사하는지(비교 분석 영향 없음) 확인 |
| 5 | web-01 CPU 급증, auth-service 커넥션 풀도 같이 확인해줘 | 기본 2개 시스템 동시 언급 — 기존 동작(web-01, auth-service 둘 다 조사) 회귀 확인 |
| 6 | payments-service랑 cache-service 둘 다 저하됐는데 공통 원인이 있는지 분석해줘 | needs_analysis=true 유도 — gate(W6)·analysis_node 비교 분석이 여전히 동작하는지 확인 |
| 7 | 로그인 응답이 느려요, 원인 확인해주세요 | 시스템명 미언급 — 임의 서비스 조회 없이 대상 서비스명을 되묻는지 확인 |
| 8 | billing-service 응답이 느려요 | 미등록 서비스 안내 및 등록 목록 표시, auth-service를 대신 조회하지 않는지 확인 |
| 9 | 안녕, 너는 누구야? | route=chat 직행(Wave 5) — 토큰 스트리밍(AnswerToken)이 채팅 경로에서도 실시간으로 오는지 확인 |
| 10 | 제 이름은 민수입니다 → (다음 메시지로) 제 이름이 뭐였죠? | Wave 12 세션 기억 — 서버 재시작 후에도 유지되는지, 스트리밍과 충돌 없는지 확인 |

## 대상 확인 개선 검증 (v2)

- 새 대화에서 `응답이 느려요. 원인 확인해주세요.` → 서비스명 확인 질문, 모니터링 조회 0건.
- 이어서 `payments-service` → 이전 증상을 유지하여 payments-service만 조회.
- 새 대화에서 `payments-service와 billing-service 지연` → 미등록 대상 안내 후 전체 진단 보류.
- 새 대화에서 `my-auth-service 지연` → auth-service로 부분 매칭하지 않음.
- 새 대화에서 `PAYMENTS-SERVICE 지연` → 등록명 payments-service로 조회.
- 두 서비스가 모두 등록되어 있으면 두 대상만 조회. 모델이 추가한 무관한 대상은 제외.
- 일반 대화는 기존 chat 경로 유지.

자동 검증: `.venv/bin/python3 -m unittest discover -s tests -v`

## 근거·조건 보존 보고서 검증 (v2)

1. `payments-service 결제 응답이 느려지고 CPU가 치솟았어요, 원인 확인해주세요`
   - CPU·메모리와 알림 원문을 관측값으로 표시하는지 확인.
   - 지속 여부·로그·근본 원인을 미확인으로 표시하는지 확인.
   - CPU 매뉴얼 3번을 선택했다면 `90%를 넘고 지속되면` 조건을 그대로 유지해야 함.
   - 느린 인스턴스명 `payments-service-3`을 새로 만들 인스턴스로 바꾸면 안 됨.
2. `auth-service 로그인 응답이 느려요, 원인 확인해주세요`
   - p95 2.4s, 커넥션 풀 92%는 알림 원문으로 표시.
   - 매뉴얼 4번을 선택했다면 `5분 내 ... 내려오지 않으면` 조건 유지.
   - 리전별 자료가 없는데 분포가 정상이라고 단정하면 안 됨.
3. 자동 검증에서는 없는 수치 추가·조건 삭제·잘못된 항목 ID·매뉴얼 미수집·3회 검증 실패도 확인.
   실패한 진단 초안이 AnswerToken으로 사용자에게 먼저 노출되지 않는지 함께 검사한다.

## 서비스별 매뉴얼 검증 (v2)

- `payments-service랑 cache-service 둘 다 저하됐는데 공통 원인이 있는지 분석해줘`
  - payments-service의 CPU 매뉴얼과 cache-service의 메모리 매뉴얼을 모두 조회.
  - 권고에 해당 서비스명을 표시하고 매뉴얼 원문의 조건 유지.
  - 두 서비스가 저하됐다는 이유만으로 공통 원인을 확정하지 않음.
- `web-01 CPU 급증, auth-service 커넥션 풀도 같이 확인해줘`
  - 현재 정상인 web-01에 CPU 장애 매뉴얼을 강제 적용하지 않음.
  - 현재 관측으로 과거 급증 여부는 확인할 수 없음을 안내.
  - 응답 지연 매뉴얼은 auth-service의 권고로 명확히 표시.
- 같은 매뉴얼이 두 서비스에 필요한 경우 원문은 한 번 조회하되 두 서비스 모두 권고 제공.
- 한 서비스에 CPU·메모리 증상이 함께 있으면 두 매뉴얼 모두 제공.
- 모델이 한 서비스의 항목만 선택하면 검증에서 거절하고 모든 매뉴얼의 기본 항목으로 대체.
