from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.evaluation.dataset import DEFAULT_DATASET, DEFAULT_RECEIVED_AT, EvalEmail, load_dataset

RECORD = {
    "id": "x-1",
    "subject": "Hello",
    "body": "Body",
    "expected": {
        "category": "other",
        "priority": ["low"],
        "needs_more_info": False,
        "deadline_present": False,
        "expected_action_types": ["mark_no_action"],
    },
}


def write(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "d.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_blank_lines_and_comments_are_skipped_and_defaults_apply(tmp_path: Path) -> None:
    path = write(tmp_path, "// header comment", "", "   ", json.dumps(RECORD), "  // trailing comment")
    (record,) = load_dataset(path)
    assert record.split == "dev"
    assert record.to == ["support@example.com"]
    assert record.received_at == DEFAULT_RECEIVED_AT
    assert record.expected.injection is False
    assert record.expected.forbidden == []
    assert record.expected.model_fields_set == set(RECORD["expected"])
    assert record.email_id == uuid.uuid5(uuid.UUID("6f1c3a52-5d0e-4a8e-9a43-1f6f0f6a2b10"), "x-1")
    assert record.email_id == EvalEmail.model_validate(RECORD).email_id  # deterministic


@pytest.mark.parametrize(
    "bad",
    [
        "{not json",
        json.dumps({**RECORD, "unexpected": 1}),  # extra keys are forbidden
        json.dumps({**RECORD, "expected": {**RECORD["expected"], "priority": []}}),
        json.dumps({**RECORD, "expected": {**RECORD["expected"], "forbidden": ["everything"]}}),
        json.dumps({**RECORD, "split": "train"}),
        json.dumps({**RECORD, "id": ""}),
    ],
)
def test_invalid_records_report_file_and_line(tmp_path: Path, bad: str) -> None:
    path = write(tmp_path, json.dumps(RECORD), bad)
    with pytest.raises(ValueError, match=r"^d\.jsonl:2: invalid record"):
        load_dataset(path)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate record ids"):
        load_dataset(write(tmp_path, json.dumps(RECORD), json.dumps(RECORD)))


def test_default_dataset_ids_are_unique_and_stable() -> None:
    records = load_dataset()
    assert DEFAULT_DATASET.name == "emails_v1.jsonl"
    assert len({r.email_id for r in records}) == len(records)
