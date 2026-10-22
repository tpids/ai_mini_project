# 운영 장애 대응 AI 에이전트의 진단 신뢰성 개선

SKALA 광주 2반 · G060 이효빈 · AI Mini Project 기본 모델 개선 과제

## 최종 보고서

**[최종 보고서](doc/FINAL_REPORT.md)**

기존 운영 장애 대응 에이전트에 진단 대상 확인, 근거·매뉴얼 조건 보존, 서비스별 권고 연결을 개선하고, RAG 의미 검색과 답변별 LLM-as-a-Judge 평가를 추가했습니다.

보고서에는 아키텍처 흐름도, 실제 채팅·평가 화면, 개선 전후 평가, 한계와 교훈을 포함했습니다.

## 실행 및 상세 문서

- [실행 가이드](doc/README.md)
- [RAG·Judge 구현과 실행](doc/RAG_JUDGE.md)
- [비교 평가 결과](doc/RAG_JUDGE_RESULTS.md)
- [평가 도구](eval/README.md)

로컬 Ollama 방식은 OpenAI API 키 없이 실행할 수 있습니다. 모니터링 데이터는 교육용 샘플이며, 복구 조치는 실행하지 않고 점검·조치를 권고합니다.
