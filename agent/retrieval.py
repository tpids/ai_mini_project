"""Ollama 의미 임베딩과 SQLite 벡터 저장소를 사용하는 소규모 매뉴얼 검색.

검색 결과는 매뉴얼 유형의 후보일 뿐 장애 원인/조치 실행의 증거가 아니다.
보고서 원문은 이후 MCP Resource에서 다시 읽어 기존 검증을 통과시킨다.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading

import httpx
from langsmith import traceable

ROOT = Path(__file__).resolve().parent.parent
INDEX_VERSION = "runbook-whole-document-v1"


def normalized(values):
    if not isinstance(values, list) or not values or any(
            type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError("유효하지 않은 임베딩")
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0:
        raise ValueError("0 벡터 임베딩")
    return [v / norm for v in values]


class OllamaEmbeddings:
    def __init__(self, model="embeddinggemma", base_url=None, timeout=45):
        self.model = model
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.timeout = timeout
        self._identity = None

    @property
    def identity(self):
        if self._identity is None:
            response = httpx.get(self.base_url + "/api/tags", timeout=self.timeout)
            response.raise_for_status()
            name = self.model if ":" in self.model else self.model + ":latest"
            match = next((m for m in response.json()["models"] if m["name"] == name), None)
            if not match:
                raise ValueError(f"임베딩 모델 미설치: {self.model}")
            self._identity = f"{name}@{match['digest']}"
        return self._identity

    @traceable(run_type="embedding", name="rag.embed")
    def embed(self, texts):
        response = httpx.post(self.base_url + "/api/embed", json={
            "model": self.model, "input": texts, "truncate": False, "keep_alive": "5m"}, timeout=self.timeout)
        response.raise_for_status()
        vectors = response.json()["embeddings"]
        if len(vectors) != len(texts):
            raise ValueError("임베딩 응답 개수 불일치")
        return [normalized(v) for v in vectors]


class RunbookRetriever:
    def __init__(self, source_root=ROOT, db_path=None, embeddings=None,
                 min_score=0.40, min_margin=0.04):
        self.source_root = Path(source_root)
        self.db_path = Path(db_path or ROOT / ".cache/runbooks.sqlite3")
        self.embeddings = embeddings or OllamaEmbeddings()
        self.min_score = min_score
        self.min_margin = min_margin
        self._lock = threading.Lock()

    def documents(self):
        docs = []
        for path in sorted((self.source_root / "data/runbooks").glob("*.md")):
            if not re.fullmatch(r"[a-z-]+", path.stem):
                continue
            body = path.read_text()
            if not re.search(r"^\d+\.\s", body, re.M):
                continue
            docs.append({"incident_type": path.stem,
                         "uri": f"runbook://incident-response/{path.stem}",
                         "sha256": hashlib.sha256(body.encode()).hexdigest(), "body": body})
        if not docs:
            raise ValueError("검색할 매뉴얼이 없습니다.")
        return docs

    def _ensure_index(self, db, docs):
        db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS vectors (incident_type TEXT PRIMARY KEY, uri TEXT NOT NULL, sha256 TEXT NOT NULL, body TEXT NOT NULL, vector TEXT NOT NULL)")
        identity = self.embeddings.identity
        signature = hashlib.sha256(json.dumps({"version": INDEX_VERSION, "model": identity,
            "documents": [(d["incident_type"], d["sha256"]) for d in docs]}, sort_keys=True).encode()).hexdigest()
        stored = db.execute("SELECT value FROM metadata WHERE key='signature'").fetchone()
        count = db.execute("SELECT count(*) FROM vectors").fetchone()[0]
        if stored and stored[0] == signature and count == len(docs):
            return identity
        vectors = self.embeddings.embed([d["body"] for d in docs])
        if len(vectors) != len(docs) or len({len(v) for v in vectors}) != 1:
            raise ValueError("문서 임베딩 차원 불일치")
        # 임베딩 실패 시 기존 DB를 삭제하지 않고, 전체 성공 후 트랜잭션으로 교체한다.
        db.execute("DELETE FROM vectors")
        db.executemany("INSERT INTO vectors VALUES (?, ?, ?, ?, ?)", [
            (d["incident_type"], d["uri"], d["sha256"], d["body"], json.dumps(normalized(v)))
            for d, v in zip(docs, vectors, strict=True)])
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('signature', ?)", (signature,))
        db.commit()
        return identity

    @traceable(run_type="retriever", name="rag.search_runbooks")
    def search(self, query, top_k=3):
        if not query.strip() or top_k < 1:
            raise ValueError("빈 검색어나 잘못된 top_k")
        with self._lock:
            docs = self.documents()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path, timeout=10) as db:
                identity = self._ensure_index(db, docs)
                query_vector = normalized(self.embeddings.embed([query])[0])
                hits = []
                for kind, uri, digest, vector in db.execute("SELECT incident_type, uri, sha256, vector FROM vectors"):
                    values = normalized(json.loads(vector))
                    if len(values) != len(query_vector):
                        raise ValueError("검색 임베딩 차원 불일치")
                    score = sum(x * y for x, y in zip(values, query_vector, strict=True))
                    hits.append({"incident_type": kind, "uri": uri, "sha256": digest,
                                 "score": round(max(-1.0, min(1.0, score)), 6)})
        hits.sort(key=lambda h: (-h["score"], h["incident_type"]))
        return {"model": identity, "hits": hits[:top_k], "index_version": INDEX_VERSION}

    def select(self, data, rule_types):
        # 정상 서비스는 사용자의 'CPU 급증' 표현만으로 매뉴얼을 강제로 붙이지 않는다.
        if data.get("status") == "healthy" and not data.get("alerts") and not rule_types:
            return {"incident_types": [], "method": "no_signal", "hits": []}
        # 다른 서비스의 증상이 섞인 사용자 질문 대신 해당 서비스의 실제 알림만 검색한다.
        parts = [str(a) for a in data.get("alerts", [])]
        for key, label in [("cpu_percent", "CPU 사용률"), ("memory_percent", "메모리 사용률")]:
            value = data.get(key)
            if isinstance(value, (int, float)) and value >= 90:
                parts.append(f"{label} {value}%")
        if data.get("status") == "down":
            parts.append("서비스가 중단됨")
        if not parts:
            return {"incident_types": list(rule_types), "method": "insufficient_evidence", "hits": []}
        result = self.search("\n".join(parts))
        hits = result["hits"]
        if rule_types:
            # 검색 순위를 활용하면서 기존 규칙이 요구한 모든 유형을 보존한다.
            selected = [h["incident_type"] for h in hits if h["incident_type"] in rule_types]
            selected += [t for t in rule_types if t not in selected]
            method = "rag_with_rule_coverage"
        else:
            margin = hits[0]["score"] - hits[1]["score"] if len(hits) > 1 else 0
            selected = [hits[0]["incident_type"]] if hits and hits[0]["score"] >= self.min_score and margin >= self.min_margin else []
            method = "rag_semantic" if selected else "rag_abstained"
        return {**result, "incident_types": selected, "method": method}


def build_retriever(source_root=ROOT, db_path=None):
    return RunbookRetriever(source_root, db_path,
        OllamaEmbeddings(os.getenv("RAG_EMBED_MODEL", "embeddinggemma")),
        float(os.getenv("RAG_MIN_SCORE", "0.40")), float(os.getenv("RAG_MIN_MARGIN", "0.04")))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="로컬 매뉴얼 벡터 인덱스 준비 및 검색")
    parser.add_argument("--query", default="운영 장애 대응 매뉴얼")
    args = parser.parse_args()
    result = build_retriever().search(args.query)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
