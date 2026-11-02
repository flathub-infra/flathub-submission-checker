from datetime import UTC, datetime

import pytest

from flathub_submission_checker.parsing import (
    _role_checklist_matches,
    checklist_fully_checked,
    checklist_matches_template,
    count_unchecked_relevant_items,
    has_missing_video,
    parse_checklist,
)
from flathub_submission_checker.validation import is_considered_spam

CHECKLIST_DATE = datetime(2026, 9, 9, tzinfo=UTC)

WRAPPED_CHECKLIST_BODY = """\
- [x] Please describe the application briefly.
- [x] Please attach a video showcasing the application on Linux using the Flatpak.
  https://example.com/demo-video.mp4
- [x] The Flatpak ID follows all the rules listed in the
  requirements.
- [x] I have read and followed all the
  submission requirements and the Submission guide.
  - [x] The application has a meaningful development history, evidence of real-world
    use, and a clear commitment to ongoing maintenance, as required by the
    development history requirements.
  - [x] I have disclosed any AI-generated material included in the application or
    its Flathub packaging, as required by the Generative AI policy. None
  - [x] I have not used AI tools or agents to generate or automate this submission
    pull request or its review interactions.
- [x] I am the original author of this project.
"""

UNCHECKED_WRAPPED_BODY = WRAPPED_CHECKLIST_BODY.replace(
    "  - [x] The application has a meaningful development history, evidence of real-world",
    "  - [ ] The application has a meaningful development history, evidence of real-world",
)


class TestParseChecklistWrapping:
    def test_indented_continuation_lines_are_folded(self):
        body = (
            "- [x] The Flatpak ID follows all the rules listed in the\n"
            "  requirements.\n"
        )
        assert parse_checklist(body) == [
            (True, "The Flatpak ID follows all the rules listed in the requirements.")
        ]

    def test_folding_stops_at_next_checklist_item(self):
        body = "- [x] First item\n  wrapped\n- [ ] Second item\n"
        assert parse_checklist(body) == [
            (True, "First item wrapped"),
            (False, "Second item"),
        ]

    def test_folding_stops_at_blank_line(self):
        body = "- [x] First item\n  wrapped\n\nNot part of the checklist.\n"
        assert parse_checklist(body) == [(True, "First item wrapped")]

    def test_lazy_continuation_is_folded(self):
        body = "- [x] First item\nNot indented.\n"
        assert parse_checklist(body) == [(True, "First item Not indented.")]

    def test_folding_stops_at_other_markdown_blocks(self):
        for block in (
            "# Heading",
            "> Quote",
            "- List item",
            "1. List item",
            "```",
            "<!-- comment -->",
        ):
            body = f"- [x] First item\n{block}\nUnrelated text\n"
            assert parse_checklist(body) == [(True, "First item")]

    def test_lazy_wrapped_video_passes_spam_check(self):
        body = WRAPPED_CHECKLIST_BODY.replace(
            "- [x] Please attach a video showcasing the application on Linux using the Flatpak.\n",
            "- [x] Please attach a video showcasing the application on Linux using the\nFlatpak.\n",
        )
        assert (
            is_considered_spam(
                parse_checklist(body),
                ["com.example.App.yml"],
                body,
                set(),
                "com.example.App",
                CHECKLIST_DATE,
            )[0]
            is False
        )

    def test_wrapped_body_parses_into_expected_items(self):
        assert len(parse_checklist(WRAPPED_CHECKLIST_BODY)) == 8


class TestWrappedChecklistTemplate:
    def test_wrapped_items_match_template(self):
        checklist = parse_checklist(WRAPPED_CHECKLIST_BODY)
        assert checklist_matches_template(checklist, CHECKLIST_DATE) is True

    def test_wrapped_checked_items_are_fully_checked(self):
        checklist = parse_checklist(WRAPPED_CHECKLIST_BODY)
        assert checklist_fully_checked(checklist, CHECKLIST_DATE) is True

    def test_wrapped_unchecked_item_is_not_fully_checked(self):
        checklist = parse_checklist(UNCHECKED_WRAPPED_BODY)
        assert checklist_matches_template(checklist, CHECKLIST_DATE) is True
        assert checklist_fully_checked(checklist, CHECKLIST_DATE) is False

    def test_wrapped_unchecked_item_is_counted(self):
        checklist = parse_checklist(UNCHECKED_WRAPPED_BODY)
        assert count_unchecked_relevant_items(checklist, CHECKLIST_DATE) == 1

    def test_wrapped_role_line_still_matches(self):
        body = "- [x] I am the original author of this\n  project.\n"
        assert _role_checklist_matches(parse_checklist(body)[0][1]) is True


@pytest.mark.parametrize(
    "role_line",
    [
        "I am the author of this application.",
        "I am the original author of this project.",
        "I am a developer of the app.",
        "I am an upstream contributor of this project.",
        "I am the author/developer/upstream contributor to the project.",
        "I am a contributor to the project, occasionally.",
        "I am a maintainer in the project.",
        "I am the maintainer of this project.",
        "I am an author to the codebase.",
        "If not, I contacted upstream developers about this submission.",
    ],
)
def test_natural_role_wording_is_accepted(role_line):
    assert _role_checklist_matches(role_line) is True


@pytest.mark.parametrize(
    "role_line",
    [
        "I am a user of this project.",
    ],
)
def test_unrelated_wording_is_rejected(role_line):
    assert _role_checklist_matches(role_line) is False


class TestWrappedVideoChecklist:
    def test_wrapped_video_item_with_link_is_not_missing(self):
        body = (
            "- [x] Please attach a video showcasing the application on Linux using the\n"
            "  Flatpak.\n"
            "  https://example.com/video.mp4\n"
        )
        assert has_missing_video(body) is False

    def test_wrapped_video_item_without_link_is_missing(self):
        body = (
            "- [x] Please attach a video showcasing the application on Linux\n"
            "  using the Flatpak.\n"
        )
        assert has_missing_video(body) is True

    def test_wrapped_unchecked_video_item_is_missing(self):
        body = (
            "- [ ] Please attach a video showcasing the application on Linux using the\n"
            "  Flatpak.\n"
            "  https://example.com/video.mp4\n"
        )
        assert has_missing_video(body) is True

    def test_wrapped_video_reference_link_is_not_missing(self):
        body = (
            "- [x] Please attach a video showcasing the application on Linux\n"
            "  using the Flatpak. [Demo Video]\n"
            "- [x] I am an author to the project.\n"
            "\n"
            "[demo video]: https://example.com/video.mp4\n"
        )
        assert has_missing_video(body) is False

    def test_wrapped_video_item_boundary_preserved(self):
        body = (
            "- [x] Please attach a video showcasing the application on Linux\n"
            "  using the Flatpak.\n"
            "- [x] I am an author to the project. Link: https://example.com/issues/1\n"
        )
        assert has_missing_video(body) is True
