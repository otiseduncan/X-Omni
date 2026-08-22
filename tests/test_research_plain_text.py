from core.services import research_conversation


def test_research_plain_text_removes_markdown_asterisks_and_keeps_words():
    raw = (
        "**Evidence Summary:** Toyota **does not approve** recycled parts.\n\n"
        "See [Toyota Collision Pros](https://example.com/source)."
    )

    value = research_conversation._plain_chat(raw)

    assert "*" not in value
    assert "**" not in value
    assert "Evidence Summary:" in value
    assert "Toyota does not approve recycled parts." in value
    assert "Toyota Collision Pros — https://example.com/source" in value


def test_research_trim_never_returns_literal_asterisks():
    value = research_conversation._trim(
        "### **Answer**\nToyota **requires** calibration after the specified condition."
    )

    assert "*" not in value
    assert "Answer" in value
    assert "Toyota requires calibration" in value
