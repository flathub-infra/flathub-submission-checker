from dataclasses import dataclass, field
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from flathub_submission_checker import parsing
from flathub_submission_checker.constants import (
    CHECKLIST_ITEMS,
    LABEL_LEAVE_OPEN,
    LABEL_MIGRATE,
    REVIEW_COMMENT_PARTIAL,
    SPAM_CLOSE_COMMENT,
)
from flathub_submission_checker.github_client import ReviewState
from flathub_submission_checker.models import PRContext
from flathub_submission_checker.parsing import (
    count_unchecked_relevant_items,
    missing_checklist_items,
    parse_checklist,
    unchecked_checklist_items,
)
from flathub_submission_checker.validation import (
    is_considered_spam,
    validate_pr_structure,
)
from flathub_submission_checker.validator import PRValidator

ROLE_LINE = "I am the author/developer/upstream contributor to the project."
CHECKLIST_DATE = datetime(2026, 9, 9, tzinfo=UTC)
VIDEO_LINK = "      https://example.com/demo-video.mp4"
VALID_FILES = [
    ".github/workflows/update_sources.yaml",
    "com.example.foobar.json",
    "cargo-sources.json",
]
APPID = "com.example.foobar"


def effective_items(created_at: datetime | None = CHECKLIST_DATE) -> list[str]:
    return [
        item
        for item, effective_date in CHECKLIST_ITEMS
        if effective_date is None
        or (created_at is not None and effective_date <= created_at)
    ]


def checklist_body(unchecked: set[int] | None = None) -> str:
    unchecked = unchecked or set()
    lines = []
    for index, item in enumerate(effective_items()):
        mark = " " if index in unchecked else "x"
        lines.append(f"- [{mark}] {item}")
        if index == 1:
            lines.append(VIDEO_LINK)
    lines.append(f"- [x] {ROLE_LINE}")
    return "\n".join(lines) + "\n"


def remove_item(body: str, item: str) -> str:
    return "\n".join(line for line in body.splitlines() if item not in line) + "\n"


@dataclass
class FakeUser:
    login: str


@dataclass
class FakeComment:
    body: str
    user: FakeUser


@dataclass
class FakeLabel:
    name: str


@dataclass
class FakeFile:
    filename: str


@dataclass
class FakeRawPR:
    number: int
    title: str | None
    body: str | None
    draft: bool
    created_at: datetime | None
    _comments: list[FakeComment] = field(default_factory=list)
    _files: list[FakeFile] = field(default_factory=list)
    _labels: list[FakeLabel] = field(default_factory=list)

    def get_issue_comments(self):
        return self._comments

    def get_files(self):
        return self._files

    def get_labels(self):
        return self._labels

    def get_commits(self):
        return []


def make_raw_pr(
    body: str = "",
    files: list[str] | None = None,
    labels: list[str] | None = None,
    comments: list[FakeComment] | None = None,
    created_at: datetime | None = CHECKLIST_DATE,
) -> FakeRawPR:
    return FakeRawPR(
        number=7378,
        title=f"Add {APPID}",
        body=body,
        draft=False,
        created_at=created_at,
        _comments=comments or [],
        _files=[FakeFile(filename=f) for f in (files or VALID_FILES)],
        _labels=[FakeLabel(name=label) for label in (labels or [])],
    )


def make_client(fetch_pr: FakeRawPR | None) -> MagicMock:
    client = MagicMock()
    client.fetch_pull_request.return_value = fetch_pr
    client.fetch_review_state.return_value = ReviewState(
        unresolved_threads=0, changes_pending=False
    )
    client.add_labels.return_value = True
    client.remove_labels.return_value = True
    client.post_comment.return_value = True
    client.close_pr.return_value = True
    client.has_human_reopened.return_value = False
    return client


def make_context(body: str, files: list[str] | None = None) -> PRContext:
    return PRContext(
        number=7378,
        title=f"Add {APPID}",
        body=body,
        is_draft=False,
        files=files if files is not None else list(VALID_FILES),
        labels=set(),
        created_at=CHECKLIST_DATE,
    )


def posted_bodies(client: MagicMock) -> list[str]:
    return [call.args[1] for call in client.post_comment.call_args_list]


class TestChecklistDiagnostics:
    def test_missing_and_unchecked_items_are_reported_separately(self):
        items = effective_items()
        missing_item = items[1]
        unchecked_item = items[2]
        checklist = [(True, ROLE_LINE)]
        for item in items:
            if item == missing_item:
                continue
            checklist.append((item != unchecked_item, item))

        assert missing_checklist_items(checklist, CHECKLIST_DATE) == [missing_item]
        assert unchecked_checklist_items(checklist, CHECKLIST_DATE) == [unchecked_item]

    def test_missing_role_item_is_reported(self):
        checklist = [(True, item) for item in effective_items()]
        missing = missing_checklist_items(checklist, CHECKLIST_DATE)
        assert len(missing) == 1
        assert "role" in missing[0].lower()

    def test_unchecked_role_item_is_reported(self):
        checklist = [(True, item) for item in effective_items()]
        checklist.append((False, ROLE_LINE))
        assert unchecked_checklist_items(checklist, CHECKLIST_DATE) == [
            parsing.ROLE_CHECKLIST_ITEM_NAME
        ]

    def test_unrecognized_unchecked_item_is_named(self):
        checklist = [(True, ROLE_LINE)]
        checklist.extend((True, item) for item in effective_items())
        checklist.append((False, "Please confirm the license."))
        assert unchecked_checklist_items(checklist, CHECKLIST_DATE) == [
            "Please confirm the license."
        ]

    def test_future_items_follow_creation_date(self, monkeypatch):
        future_item = "Future submission requirement."
        effective_date = datetime(2026, 10, 1, tzinfo=UTC)
        monkeypatch.setattr(
            parsing,
            "CHECKLIST_ITEMS",
            (*CHECKLIST_ITEMS, (future_item, effective_date)),
        )
        before = datetime(2026, 9, 10, tzinfo=UTC)
        checked = [(True, ROLE_LINE)]
        checked.extend((True, item) for item in effective_items(before))

        assert missing_checklist_items(checked, before) == []
        assert unchecked_checklist_items(checked, before) == []
        assert missing_checklist_items(checked, effective_date) == [future_item]

        unchecked = [*checked, (False, future_item)]
        assert unchecked_checklist_items(unchecked, before) == []
        assert unchecked_checklist_items(unchecked, effective_date) == [future_item]


class TestSpamDiagnostics:
    def test_nested_files_message_says_what_to_move(self):
        body = checklist_body()
        files = ["some/nested/file.json", "another/nested/file.yaml"]
        spam, diagnostics = is_considered_spam(
            parse_checklist(body), files, body, set(), APPID, CHECKLIST_DATE
        )
        assert spam is True
        assert len(diagnostics) == 1
        assert "Move the Flatpak manifest" in diagnostics[0]
        assert "top level" in diagnostics[0]
        assert "nested files are not accepted" not in diagnostics[0]

    def test_missing_item_message_names_the_item(self):
        item = effective_items()[-1]
        body = remove_item(checklist_body(), item)
        spam, diagnostics = is_considered_spam(
            parse_checklist(body), VALID_FILES, body, set(), APPID, CHECKLIST_DATE
        )
        assert spam is True
        assert any("Add the missing checklist item(s)" in d for d in diagnostics)
        assert any(item in d for d in diagnostics)

    def test_too_many_unchecked_items_are_named(self):
        items = effective_items()
        body = checklist_body(unchecked={0, 2})
        spam, diagnostics = is_considered_spam(
            parse_checklist(body), VALID_FILES, body, set(), APPID, CHECKLIST_DATE
        )
        assert spam is True
        assert len(diagnostics) == 1
        assert "Complete and check the unchecked checklist item(s)" in diagnostics[0]
        assert items[0] in diagnostics[0]
        assert items[2] in diagnostics[0]

    def test_video_message_says_what_to_add(self):
        body = checklist_body().replace(VIDEO_LINK + "\n", "")
        spam, diagnostics = is_considered_spam(
            parse_checklist(body), VALID_FILES, body, set(), APPID, CHECKLIST_DATE
        )
        assert spam is True
        assert len(diagnostics) == 1
        assert "attach a video" in diagnostics[0]

    def test_one_unchecked_item_is_not_spam(self):
        body = checklist_body(unchecked={0})
        assert is_considered_spam(
            parse_checklist(body), VALID_FILES, body, set(), APPID, CHECKLIST_DATE
        ) == (False, [])

    @pytest.mark.parametrize(
        ("labels", "appid"),
        [
            ({LABEL_MIGRATE}, APPID),
            (set(), "org.freedesktop.Sdk.Extension.foobar"),
        ],
    )
    def test_video_exemptions_have_no_diagnostics(self, labels, appid):
        body = checklist_body().replace(VIDEO_LINK + "\n", "")
        assert is_considered_spam(
            parse_checklist(body), VALID_FILES, body, labels, appid, CHECKLIST_DATE
        ) == (False, [])

    def test_multiple_failures_are_all_reported(self):
        items = effective_items()
        body = remove_item(checklist_body().replace(VIDEO_LINK + "\n", ""), items[-1])
        files = ["nested/file.json", "nested/file.yaml"]
        spam, diagnostics = is_considered_spam(
            parse_checklist(body), files, body, set(), APPID, CHECKLIST_DATE
        )
        assert spam is True
        assert len(diagnostics) == 3
        assert any("top level" in d for d in diagnostics)
        assert any("Add the missing checklist item(s)" in d for d in diagnostics)
        assert any("attach a video" in d for d in diagnostics)

    def test_date_rules_skip_future_failures(self, monkeypatch):
        future_item = "Future submission requirement."
        effective_date = datetime(2026, 10, 1, tzinfo=UTC)
        monkeypatch.setattr(
            parsing,
            "CHECKLIST_ITEMS",
            (*CHECKLIST_ITEMS, (future_item, effective_date)),
        )
        before = datetime(2026, 9, 10, tzinfo=UTC)
        body = checklist_body(unchecked={0}) + f"- [ ] {future_item}\n"
        checklist = parse_checklist(body)

        assert count_unchecked_relevant_items(checklist, before) == 1
        assert is_considered_spam(
            checklist, VALID_FILES, body, set(), APPID, before
        ) == (False, [])
        assert (
            is_considered_spam(
                checklist, VALID_FILES, body, set(), APPID, effective_date
            )[0]
            is True
        )


class TestClosureComment:
    def test_closure_comment_has_actionable_diagnostics(self):
        body = checklist_body()
        files = ["nested/file.json", "nested/file.yaml"]
        client = make_client(make_raw_pr(body=body, files=files))

        assert PRValidator(client, "flathub/flathub").validate_pr(7378) is True

        posted = posted_bodies(client)
        assert len(posted) == 1
        assert SPAM_CLOSE_COMMENT in posted[0]
        assert "Diagnostics:" in posted[0]
        assert "top level" in posted[0]
        assert "Files not in toplevel" not in posted[0]
        client.close_pr.assert_called_once_with(7378)

    def test_duplicate_closure_comment_is_not_posted(self):
        body = checklist_body()
        files = ["nested/file.json", "nested/file.yaml"]
        _, diagnostics = is_considered_spam(
            parse_checklist(body), files, body, set(), APPID, CHECKLIST_DATE
        )
        previous_comment = f"{SPAM_CLOSE_COMMENT} Diagnostics: {' '.join(diagnostics)}"
        client = make_client(
            make_raw_pr(
                body=body,
                files=files,
                comments=[
                    FakeComment(
                        body=previous_comment,
                        user=FakeUser(login="github-actions"),
                    )
                ],
            )
        )

        assert PRValidator(client, "flathub/flathub").validate_pr(7378) is True

        client.post_comment.assert_not_called()
        client.close_pr.assert_called_once_with(7378)

    def test_missing_item_closure_comment_names_item(self):
        item = effective_items()[0]
        body = remove_item(checklist_body(), item)
        client = make_client(make_raw_pr(body=body))

        assert PRValidator(client, "flathub/flathub").validate_pr(7378) is True

        posted = posted_bodies(client)
        assert len(posted) == 1
        assert "Add the missing checklist item(s)" in posted[0]
        assert item in posted[0]

    def test_video_closure_comment_says_what_to_add(self):
        body = checklist_body().replace(VIDEO_LINK + "\n", "")
        client = make_client(make_raw_pr(body=body))

        assert PRValidator(client, "flathub/flathub").validate_pr(7378) is True

        posted = posted_bodies(client)
        assert len(posted) == 1
        assert "attach a video" in posted[0]
        assert "Video checklist requirement not met" not in posted[0]
        client.close_pr.assert_called_once_with(7378)


class TestBlockedReviewComment:
    def test_blocked_review_names_unchecked_item(self):
        item = effective_items()[0]
        body = checklist_body(unchecked={0})
        client = make_client(make_raw_pr(body=body))

        assert PRValidator(client, "flathub/flathub").validate_pr(7378) is True

        review_comments = [
            posted
            for posted in posted_bodies(client)
            if REVIEW_COMMENT_PARTIAL in posted
        ]
        assert len(review_comments) == 1
        assert "Complete and check the [checklists]" in review_comments[0]
        assert item in review_comments[0]

    def test_leave_open_missing_item_reaches_blocked_review(self):
        item = effective_items()[-1]
        body = remove_item(checklist_body(), item)
        client = make_client(make_raw_pr(body=body, labels=[LABEL_LEAVE_OPEN]))

        assert PRValidator(client, "flathub/flathub").validate_pr(7378) is True

        client.close_pr.assert_not_called()
        review_comments = [
            posted
            for posted in posted_bodies(client)
            if REVIEW_COMMENT_PARTIAL in posted
        ]
        assert len(review_comments) == 1
        assert "Add the missing [checklists]" in review_comments[0]
        assert item in review_comments[0]

    def test_blocked_review_reasons_do_not_include_generic_checklist_text(self):
        body = checklist_body(unchecked={0})
        result = validate_pr_structure(make_context(body), parse_checklist(body), APPID)
        assert result.is_valid is False
        assert any("Complete and check" in reason for reason in result.reasons)
        assert not any("All [checklists]" in reason for reason in result.reasons)
