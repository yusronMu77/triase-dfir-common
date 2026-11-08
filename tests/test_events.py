from __future__ import annotations

import pytest
from pydantic import ValidationError

from astria_common.events import UnknownEventType, decode, encode, new_event, subject_for
from astria_contracts import example


def test_new_event_validates_and_roundtrips() -> None:
    data = example("evidence.ingested")["data"]
    env = new_event("evidence.ingested", data, correlation={"job_id": "ING-1", "source_id": "hb01"})
    assert env.version == 1
    assert len(env.trace_id) == 32
    assert env.data["record_range"] == {"from": 1, "to": 4236}

    parsed, model = decode(encode(env))
    assert parsed.id == env.id
    assert model.record_range.from_ == 1  # type: ignore[attr-defined]


def test_new_event_rejects_invalid_data() -> None:
    with pytest.raises(ValidationError):
        new_event("evidence.ingested", {"source_id": "hb01"})


def test_unknown_event_type() -> None:
    with pytest.raises(UnknownEventType):
        new_event("nope.created", {})
    with pytest.raises(UnknownEventType):
        subject_for("nope.created")


@pytest.mark.parametrize(
    "event_type", ["evidence.ingested", "card.skeleton.created", "narration.completed", "card.verified", "audit.record"]
)
def test_contract_examples_decode(event_type: str) -> None:
    import orjson

    envelope, _ = decode(orjson.dumps(example(event_type)))
    assert subject_for(envelope.type) == f"astria.{event_type}"
