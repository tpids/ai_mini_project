#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python3 ] || { echo "먼저 bash sh/setup_venv.sh를 실행해주세요."; exit 1; }
command -v ollama >/dev/null || { echo "Ollama 설치가 필요합니다."; exit 1; }
started_ollama=""
cleanup() { if [ -n "$started_ollama" ]; then kill "$started_ollama" 2>/dev/null || true; fi; }
trap cleanup EXIT
if ! curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  ollama serve > /tmp/ai_mini_rag_setup.log 2>&1 &
  started_ollama=$!
  for attempt in {1..30}; do
    if curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then break; fi
    sleep 0.5
  done
fi
model="${RAG_EMBED_MODEL:-embeddinggemma}"
if ! ollama show "$model" >/dev/null 2>&1; then
  echo "RAG 임베딩 모델을 받습니다: $model (기본 embeddinggemma 약 622MB)"
  ollama pull "$model"
fi
.venv/bin/python3 -m agent.retrieval
printf '\nRAG 준비 완료. bash sh/run_agent_ollma.sh로 실행하면 기본 활성화됩니다.\n'
