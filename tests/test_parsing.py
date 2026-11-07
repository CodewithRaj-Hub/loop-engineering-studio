from reqreview.agent import (bullets, first_ambiguity, is_complete, section_counts, sections_written,
                             split_sections, summary_words)

MD = """# Requirements Analysis

## Confirmed Facts
- a
## Ambiguities
- **"x"**
  Reason: r
- **"y"**
  Reason: r2
## Clarification Questions
- q
## Assumptions
- s
## Summary
Two sentences here.
"""


def test_split_and_counts():
    assert set(split_sections(MD)) >= {"Confirmed Facts", "Ambiguities", "Summary"}
    assert section_counts(MD)["Ambiguities"] == 2
    assert summary_words(MD) == 3
    assert is_complete(MD)


def test_bullets_and_first_ambiguity():
    assert bullets("- a\n* b\ntext") == ["a", "b"]
    assert first_ambiguity(MD, 1).startswith('- **"x"**')
    assert "y" not in first_ambiguity(MD, 1)


def test_sections_written_ignores_empty_skeleton():
    assert sections_written({"content": "## Confirmed Facts\n\n## Ambiguities\n"}) == []
    assert sections_written({"content": "## Confirmed Facts\n- fact\n"}) == ["Confirmed Facts"]
