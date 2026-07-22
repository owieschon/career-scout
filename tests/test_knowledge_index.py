from alice.llm import llm


def test_first_summary_line_skips_sourcebound_markers(tmp_path):
    page = tmp_path / "page.md"
    page.write_text(
        "# Page\n\n"
        "<!-- sourcebound:purpose -->\n"
        "Unverified hypothesis notebook: example summary.\n"
        "<!-- sourcebound:end purpose -->\n"
    )

    assert llm._first_summary_line(page) == (
        "Unverified hypothesis notebook: example summary."
    )
