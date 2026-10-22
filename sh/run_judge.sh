#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python3 ] || { echo "프로젝트 실행 환경이 없습니다."; exit 1; }
exec .venv/bin/python3 -m eval.judge_results "$@"
