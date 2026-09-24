"""Unit tests for HITL risk classifier."""

from __future__ import annotations

import pytest

from app.services.hitl import classify_risk


@pytest.mark.parametrize(
    "answer,confidence,expected_risk,expected_hitl",
    [
        # HIGH risk — action verb
        ("You should repair the corrosion on segment 4B immediately.", 0.9, "HIGH", True),
        ("Recommend to shut in the pipeline pending inspection.", 0.9, "HIGH", True),
        ("Consider pressure reduction on the affected segment.", 0.9, "HIGH", True),
        ("Evacuate the area around milepost 34.", 0.9, "HIGH", True),
        # HIGH risk — fatality reference
        ("This incident resulted in 2 fatalities and 5 injuries.", 0.9, "HIGH", True),
        # MEDIUM risk — low confidence
        ("The incident rate in Texas has been stable.", 0.6, "MEDIUM", True),
        # MEDIUM risk — HCA reference
        ("This segment passes through a High Consequence Area.", 0.9, "MEDIUM", True),
        # LOW risk — factual, high confidence
        ("There were 47 hazardous liquid incidents in Texas in 2022.", 0.95, "LOW", False),
        ("The top cause of incidents was corrosion at 34%.", 0.88, "LOW", False),
    ],
)
def test_classify_risk(answer, confidence, expected_risk, expected_hitl):
    risk_level, hitl_required = classify_risk(answer, confidence)
    assert risk_level == expected_risk
    assert hitl_required == expected_hitl
