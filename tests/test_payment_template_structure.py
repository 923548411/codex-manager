from pathlib import Path


def test_country_row_divs_are_balanced() -> None:
    text = Path("templates/payment.html").read_text(encoding="utf-8")
    start = text.index("<!-- 国家选择 -->")
    end = text.index("<!-- Team 额外参数 -->")
    section = text[start:end]

    assert section.count("<div") == section.count("</div>")
