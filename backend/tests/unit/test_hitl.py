"""Unit tests for HITL risk classifier."""

from __future__ import annotations

import pytest

from app.services.hitl import classify_risk

FACT_QUESTION = "What happened in the June 2024 rupture on Line XC?"


def risk(answer: str, confidence: float = 1.0, question: str = FACT_QUESTION, recommends_action: bool = False):
    return classify_risk(question, answer, confidence, recommends_action)


@pytest.mark.parametrize(
    "answer,confidence,expected_risk,expected_hitl",
    [
        # HIGH risk — action verb
        ("You should repair the corrosion on segment 4B immediately.", 0.9, "HIGH", True),
        ("Recommend to shut in the pipeline pending inspection.", 0.9, "HIGH", True),
        ("Consider pressure reduction on the affected segment.", 0.9, "HIGH", True),
        ("Evacuate the area around milepost 34.", 0.9, "HIGH", True),
        # LOW risk — reported harm to people is a fact, not advice
        ("This incident resulted in 2 fatalities and 5 injuries.", 0.9, "LOW", False),
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
    risk_level, hitl_required = risk(answer, confidence)
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
    assert risk(answer) == ("HIGH", True)


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
    assert risk(answer) == ("LOW", False)


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
    assert risk(answer) == ("LOW", False)


def test_specific_advice_next_to_a_disclaimer_is_still_held():
    answer = (
        "We recommend reducing operating pressure to 600 psig [SRC-001]. "
        "Any such actions should be reviewed by a qualified pipeline integrity engineer."
    )
    assert risk(answer) == ("HIGH", True)


@pytest.mark.parametrize(
    "answer",
    [
        "One contractor was injured [SRC-001].",
        "The rupture caused 2 fatalities [SRC-001].",
        "There were casualties at the site [SRC-001].",
        "No fatalities occurred and no injuries required inpatient hospitalisation [SRC-001].",
        "Fatalities: 0. Injuries: 0. [SRC-001]",
        "### Injuries and Fatalities\nOne worker was injured and hospitalized [SRC-001].",
        # The first demo question (Line XC, June 2024): the record reports one minor injury.
        "- No injuries requiring inpatient hospitalization or deaths. One contractor employee received "
        "treatment on site for a minor cut [SRC-001].",
        "- **Casualties:** No fatalities. One contractor employee was injured with a minor cut and "
        "treated on site [SRC-001].",
        "- One residence about 900 feet away was evacuated as a precaution until the fire was extinguished [SRC-001].",
    ],
)
def test_reported_harm_to_people_is_not_held(answer):
    """Casualties are facts about what happened; only advice goes to an engineer."""
    assert risk(answer) == ("LOW", False)


def test_model_check_holds_advice_the_patterns_miss():
    answer = "Running Line XC at 80% of MAOP would be the cautious choice [SRC-001]."
    assert risk(answer) == ("LOW", False)
    assert risk(answer, recommends_action=True) == ("HIGH", True)


@pytest.mark.parametrize(
    "question",
    [
        "Should Southern Star reduce operating pressure on the line where the most recent corrosion incident happened?",
        "Is it safe to keep operating Line XC at 690 psig?",
        "What actions do you recommend for the Noble County segment?",
    ],
)
def test_question_asking_for_advice_is_held(question):
    assert risk("Line XC operated at 458 psig against an MAOP of 690 psig [SRC-001].", question=question) == (
        "HIGH",
        True,
    )


def test_medium_keywords_only_count_in_recommendations():
    assert risk("The leak was in a Class 3, HCA location [SRC-003].") == ("LOW", False)
    assert risk("The operator should schedule inspection of the HCA segment.") == ("MEDIUM", True)


def test_low_confidence_is_medium_risk():
    assert risk("The cause was external corrosion [SRC-001].", 0.5) == ("MEDIUM", True)
