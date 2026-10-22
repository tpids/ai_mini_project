"""실제로 공개한 채팅 답변과 그때의 수집 근거를 불변 스냅샷으로 저장한다."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ChatEvaluationStore:
    def __init__(self, directory=None):
        self.directory = Path(directory or ROOT / ".cache/chat_evaluations")

    def capture(self, message, result, models):
        facts = result.get("collected", [])
        if not result.get("final") or not any(c.get("source") == "monitoring" for c in facts):
            return None
        snapshot_id = uuid.uuid4().hex
        payload = {"id": snapshot_id, "created_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
                   "user_message": message, "query": result.get("query", message),
                   "reply": result["final"], "collected": facts, "models": models,
                   "report_mode": result.get("report_mode"), "report_step_ids": result.get("report_step_ids", [])}
        payload["content_sha256"] = digest(payload)
        self.directory.mkdir(parents=True, exist_ok=True)
        # 새로운 ID에만 생성하며 과거 답변을 덮어쓰지 않는다.
        with (self.directory / f"{snapshot_id}.json").open("x") as file:
            file.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return snapshot_id

    def load(self, snapshot_id):
        if not re.fullmatch(r"[a-f0-9]{32}", snapshot_id):
            raise ValueError("유효하지 않은 답변 ID")
        payload = json.loads((self.directory / f"{snapshot_id}.json").read_text())
        expected = payload.pop("content_sha256")
        if payload.get("id") != snapshot_id or digest(payload) != expected:
            raise ValueError("저장된 답변 또는 근거가 변경됐습니다.")
        return {**payload, "content_sha256": expected}

    def save_judgment(self, snapshot, judgment):
        output = self.directory / "judgments"
        output.mkdir(parents=True, exist_ok=True)
        record = {"snapshot_id": snapshot["id"], "snapshot_sha256": snapshot["content_sha256"],
                  "evaluated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), "judgment": judgment}
        with (output / f"{snapshot['id']}_{uuid.uuid4().hex}.json").open("x") as file:
            file.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        return record
