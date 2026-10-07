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
        # MEDIUM risk — HCA in a recommendation; a factual HCA mention is LOW
        ("Schedule inspection of the segment in the High Consequence Area.", 0.9, "MEDIUM", True),
        ("This segment passes through a High Consequence Area.", 0.9, "LOW", False),
        # LOW risk — factual, high confidence
        ("There were 47 hazardous liquid incidents in Texas in 2022.", 0.95, "LOW", False),
        ("The top cause of incidents was corrosion at 34%.", 0.88, "LOW", False),
    ],
)
def test_classify_risk(answer, confidence, expected_risk, expected_hitl):
    risk_level, hitl_required = classify_risk(answer, confidence)
    assert risk_level == expected_risk
    assert hitl_required == expected_hitl


@pytest.mark.parametrize(
    "answer",
    [
        "Repair the anomaly at odometer 2890.2 m immediately [SRC-001].",
        "- **Shut in** the line at MP 9.34 [SRC-001].",
        "Southern Star should consider reducing operating pressure on Line R [SRC-003].",
        "We recommend a 20% pressure reduction until the dig is complete [SRC-002].",
        "Pressure is 707 psig [SRC-001]; reduce pressure to 600 psig before the run.",
        "## Recommendations\n\n- Pressure reduction to 80% of MAOP on Line XC [SRC-001]",
    ],
)
def test_recommended_actions_are_high_risk(answer):
    assert classify_risk(answer, 1.0) == ("HIGH", True)


def test_described_past_actions_are_not_held():
    # From a real held answer: a factual incident account, no advice.
    answer = (
        "## Operator Response\n\n"
        "- **Ignition and explosion** at the time of the rupture (10:27 AM) [SRC-001]\n"
        "- A **pipeline shutdown**, recorded at **12:47 PM** on the same day [SRC-001]\n"
        "- The line was isolated and the repair involved 189 feet of new pipe [SRC-001]\n"
        "- Repair was completed before return to service [SRC-001]\n"
        "- **Shutdown:** yes [SRC-001]"
    )
    assert classify_risk(answer, 1.0) == ("LOW", False)


@pytest.mark.parametrize(
    "answer",
    [
        "> Any decisions regarding return-to-service, repair scope, or preventive measures "
        "arising from this incident should be reviewed and approved by a qualified pipeline "
        "integrity engineer.",
        # From a real held answer
        "> ⚠️ **Note:** Any operational, repair, or procedural recommendations arising from this "
        "incident analysis should be reviewed and validated by a qualified pipeline integrity "
        "engineer before implementation.",
    ],
)
def test_generic_engineer_review_disclaimer_is_not_a_recommendation(answer):
    assert classify_risk(answer, 1.0) == ("LOW", False)


def test_specific_advice_next_to_a_disclaimer_is_still_held():
    answer = (
        "We recommend reducing operating pressure to 600 psig [SRC-001]. "
        "Any such actions should be reviewed by a qualified pipeline integrity engineer."
    )
    assert classify_risk(answer, 1.0) == ("HIGH", True)


@pytest.mark.parametrize(
    "answer",
    [
        "One contractor was injured [SRC-001].",
        "The rupture caused 2 fatalities [SRC-001].",
        "There were casualties at the site [SRC-001].",
    ],
)
def test_harm_to_people_is_high_risk(answer):
    assert classify_risk(answer, 1.0) == ("HIGH", True)


@pytest.mark.parametrize(
    "answer",
    [
        "No fatalities occurred and no injuries required inpatient hospitalisation [SRC-001].",
        "There were no injuries or fatalities [SRC-001].",
        "Fatalities: 0. Injuries: 0. [SRC-001]",
        "- **Fatalities:** 0 [SRC-001]",
        "| Fatalities | 0 |\n| Injuries | 0 |",
        # From a real held answer: the section heading named the topic, the text negated it.
        "### Injuries and Fatalities\nThere were no fatalities and no injuries requiring "
        "inpatient hospitalization [SRC-001].",
        "**Injuries and Fatalities:**\nThere were no injuries or fatalities [SRC-001].",
    ],
)
def test_negated_harm_is_not_held(answer):
    assert classify_risk(answer, 1.0) == ("LOW", False)


def test_harm_under_a_heading_is_still_held():
    answer = "### Injuries and Fatalities\nOne worker was injured and hospitalized [SRC-001]."
    assert classify_risk(answer, 1.0) == ("HIGH", True)


def test_medium_keywords_only_count_in_recommendations():
    assert classify_risk("The leak was in a Class 3, HCA location [SRC-003].", 1.0) == ("LOW", False)
    assert classify_risk("The operator should schedule inspection of the HCA segment.", 1.0) == ("MEDIUM", True)


def test_low_confidence_is_medium_risk():
    assert classify_risk("The cause was external corrosion [SRC-001].", 0.5) == ("MEDIUM", True)
