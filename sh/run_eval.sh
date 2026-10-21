#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python3 ]; then
  echo "프로젝트 실행 환경이 없습니다. bash sh/setup_venv.sh를 먼저 실행해주세요."
  exit 1
fi
# 기본은 과금 없는 모의 모델 비교. 뒤에 지정한 CLI 인수로 변경 가능.
exec .venv/bin/python3 -m eval.run_eval --baseline-ref 741cd7e "$@"
