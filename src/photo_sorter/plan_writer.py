from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PlanItem:
    source: str
    sha: str
    size: int
    exif: dict[str, Any]
    kind: str  # file (for now)
    reference_datetime: str | None
    action: str  # copy|move|skip|quarantine|review
    destination: str | None
    reason: str
    bucket_key: str | None = None
    confidence: float | None = None


def _json_default(obj: object):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def write_plan_jsonl(path: Path, items: list[PlanItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(asdict(item), ensure_ascii=False, default=_json_default))
            f.write("\n")


def read_plan_jsonl(path: Path) -> list[PlanItem]:
    items: list[PlanItem] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if "destination" not in payload:
                payload["destination"] = None
            if "bucket_key" not in payload:
                payload["bucket_key"] = None
            if "confidence" not in payload:
                payload["confidence"] = None
            items.append(PlanItem(**payload))
    return items
