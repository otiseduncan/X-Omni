from core.services import research_procedure_followup


def test_exact_procedure_followup_is_detected():
    assert research_procedure_followup.procedure_followup(
        "can you show me the exact procedure"
    ) is True


def test_unrelated_question_is_not_hijacked():
    assert research_procedure_followup.procedure_followup(
        "what is the weather tomorrow"
    ) is False


def test_forward_camera_followup_keeps_transit_context_and_forces_exact_sources():
    history = [
        {
            "role": "user",
            "content": "I need the forward facing calibration procedure for a 24 Ford Transit",
        },
        {
            "role": "assistant",
            "content": "I found general Ford calibration information.",
        },
        {
            "role": "user",
            "content": "can you show me the exact procedure",
        },
    ]

    resolved = research_procedure_followup.resolve_followup(
        "can you show me the exact procedure", history
    )

    assert "24 Ford Transit" in resolved
    assert "forward facing calibration procedure" in resolved
    assert "exact OEM calibration procedure" in resolved
    assert "authenticated ALLDATA Repair/Collision vehicle workflow" in resolved
    assert "Do not substitute procedures from another model or year" in resolved


def test_jeep_procedure_followup_keeps_bsm_context():
    history = [
        {
            "role": "user",
            "content": "I need the BSM calibration procedure for a 21 Jeep Cherokee Latitude Luxe",
        },
        {"role": "user", "content": "show me the exact procedure"},
    ]
    resolved = research_procedure_followup.resolve_followup(
        "show me the exact procedure", history
    )
    assert "21 Jeep Cherokee Latitude Luxe" in resolved
    assert "BSM calibration procedure" in resolved
