#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python3 ] || { echo "프로젝트 실행 환경이 없습니다."; exit 1; }
# RAG 추가 전, 세 가지 개선과 평가 도구까지 포함한 커밋을 비교 대상으로 사용한다.
exec .venv/bin/python3 -m eval.run_eval --baseline-ref 68ddee4 --backend fake --retrieval rag "$@"
