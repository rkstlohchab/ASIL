import pytest
from asil_core.confidence import Confidence


def test_basic_construction() -> None:
    c = Confidence(score=0.8, evidence_count=3, retrieval_strength=0.7, derivation=["a", "b"])
    assert c.score == 0.8
    assert c.evidence_count == 3
    assert c.derivation == ["a", "b"]


def test_unknown_factory() -> None:
    c = Confidence.unknown()
    assert c.score == 0.0
    assert c.evidence_count == 0
    assert c.derivation == ["no evidence"]


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_rejects_out_of_range_score(bad: float) -> None:
    with pytest.raises(ValueError, match="score must be in"):
        Confidence(score=bad, evidence_count=0)


def test_rejects_negative_evidence_count() -> None:
    with pytest.raises(ValueError, match="evidence_count"):
        Confidence(score=0.5, evidence_count=-1)
