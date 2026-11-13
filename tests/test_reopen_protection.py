from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from github.GithubException import GithubException

from flathub_submission_checker.constants import (
    LABEL_LEAVE_OPEN,
    LABEL_PR_CHECK_BLOCKED,
    REVIEW_COMMENT_PARTIAL,
    SPAM_CLOSE_COMMENT,
)
from flathub_submission_checker.github_client import (
    ISSUE_EVENTS_PER_PAGE,
    PyGithubClient,
    ReviewState,
    is_bot_actor,
)
from flathub_submission_checker.validator import PRValidator

CUTOFF_ELIGIBLE = datetime(2026, 9, 9, tzinfo=UTC)
CUTOFF_INELIGIBLE = datetime(2026, 9, 8, tzinfo=UTC)


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
    number: int = 7378
    title: str | None = "Not a valid title"
    body: str | None = "No checklist at all."
    draft: bool = False
    created_at: datetime | None = CUTOFF_ELIGIBLE
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


def make_spam_pr(
    created_at: datetime | None = CUTOFF_ELIGIBLE,
    labels: list[str] | None = None,
) -> FakeRawPR:
    return FakeRawPR(
        number=7378,
        title="Not a valid title",
        body="No checklist at all.",
        created_at=created_at,
        _files=[FakeFile(filename="subdir/file.json")],
        _labels=[FakeLabel(name=name) for name in (labels or [])],
    )


def make_validator(
    reopen_result: bool | None,
    raw_pr: FakeRawPR | None = None,
) -> tuple[PRValidator, MagicMock]:
    client = MagicMock()
    client.fetch_pull_request.return_value = raw_pr or make_spam_pr()
    client.has_human_reopened.return_value = reopen_result
    client.fetch_review_state.return_value = ReviewState(
        unresolved_threads=0, changes_pending=False
    )
    client.add_labels.return_value = True
    client.remove_labels.return_value = True
    client.post_comment.return_value = True
    client.close_pr.return_value = True
    return PRValidator(client, "flathub/flathub"), client


def make_gh_client(
    pages: list[list[Any]] | None = None,
) -> tuple[PyGithubClient, MagicMock]:
    client = PyGithubClient.__new__(PyGithubClient)
    client.owner_login = "flathub"
    client.repo_name = "flathub"
    requester = MagicMock()
    gh = cast(Any, MagicMock())
    gh.requester = requester
    client.gh = gh
    if pages is not None:
        requester.requestJsonAndCheck.side_effect = [(None, page) for page in pages]
    return client, requester


class TestIsBotActor:
    @pytest.mark.parametrize(
        ("actor", "expected"),
        [
            ({"login": "alice", "type": "User"}, False),
            ({"login": "dependabot[bot]", "type": "User"}, True),
            ({"login": "flathubbot", "type": "User"}, True),
            ({"login": "github-actions[bot]", "type": "User"}, True),
            ({"login": "somebot", "type": "Bot"}, True),
            ({"login": "somebot", "type": "bot"}, True),
            ({"login": "alice"}, None),
            ({"type": "User"}, None),
            ({"login": "ghost", "type": "Mannequin"}, None),
            ({"login": "org", "type": "Organization"}, None),
            (None, None),
            ({}, None),
        ],
    )
    def test_classifies_actor(self, actor, expected):
        assert is_bot_actor(actor) is expected


class TestHasHumanReopened:
    def _events_url(self, pr_number: int = 7378) -> str:
        return f"/repos/flathub/flathub/issues/{pr_number}/events"

    def test_no_events_returns_false(self):
        client, _requester = make_gh_client([[]])
        assert client.has_human_reopened(7378) is False

    def test_no_reopened_event_returns_false(self):
        client, _requester = make_gh_client(
            [[{"event": "closed", "actor": {"login": "alice", "type": "User"}}]]
        )
        assert client.has_human_reopened(7378) is False

    def test_bot_reopen_only_returns_false(self):
        client, _requester = make_gh_client(
            [
                [
                    {
                        "event": "reopened",
                        "actor": {"login": "flathubbot", "type": "User"},
                    }
                ]
            ]
        )
        assert client.has_human_reopened(7378) is False

    def test_human_reopen_returns_true(self):
        client, _requester = make_gh_client(
            [[{"event": "reopened", "actor": {"login": "alice", "type": "User"}}]]
        )
        assert client.has_human_reopened(7378) is True

    def test_human_reopen_wins_over_bot_reopen(self):
        client, _requester = make_gh_client(
            [
                [
                    {
                        "event": "reopened",
                        "actor": {"login": "github-actions[bot]", "type": "Bot"},
                    },
                    {
                        "event": "reopened",
                        "actor": {"login": "alice", "type": "User"},
                    },
                ]
            ]
        )
        assert client.has_human_reopened(7378) is True

    def test_paginates_until_human_reopen_found(self):
        first_page = [{"event": "closed"}] * ISSUE_EVENTS_PER_PAGE
        client, requester = make_gh_client(
            [
                first_page,
                [{"event": "reopened", "actor": {"login": "alice", "type": "User"}}],
            ]
        )

        assert client.has_human_reopened(7378) is True

        calls = requester.requestJsonAndCheck.call_args_list
        assert [c.kwargs["parameters"]["page"] for c in calls] == [1, 2]
        assert all(
            c.args[0] == "GET" and c.args[1] == self._events_url() for c in calls
        )
        assert all(
            c.kwargs["parameters"]["per_page"] == ISSUE_EVENTS_PER_PAGE for c in calls
        )

    def test_paginates_past_bot_reopens(self):
        first_page = [
            {"event": "reopened", "actor": {"login": "flathubbot", "type": "User"}}
        ] * ISSUE_EVENTS_PER_PAGE
        client, requester = make_gh_client([first_page, []])

        assert client.has_human_reopened(7378) is False
        assert requester.requestJsonAndCheck.call_count == 2

    def test_unknown_actor_fails_safe(self):
        client, _requester = make_gh_client([[{"event": "reopened"}]])
        assert client.has_human_reopened(7378) is None

    def test_malformed_event_fails_safe(self):
        client, _requester = make_gh_client([["not-an-event-dict"]])
        assert client.has_human_reopened(7378) is None

    def test_unknown_actor_after_known_bot_reopen_fails_safe(self):
        client, _requester = make_gh_client(
            [
                [
                    {
                        "event": "reopened",
                        "actor": {"login": "flathubbot", "type": "User"},
                    },
                    {
                        "event": "reopened",
                        "actor": {"login": "ghost", "type": "Mannequin"},
                    },
                ]
            ]
        )
        assert client.has_human_reopened(7378) is None

    def test_lookup_failure_returns_none(self):
        client, requester = make_gh_client()
        requester.requestJsonAndCheck.side_effect = GithubException(
            500, {"message": "boom"}, None
        )
        assert client.has_human_reopened(7378) is None

    def test_unexpected_response_returns_none(self):
        client, requester = make_gh_client()
        requester.requestJsonAndCheck.return_value = (None, {"nope": 1})
        assert client.has_human_reopened(7378) is None


class TestProtectedReopenValidation:
    def test_human_reopen_skips_close_and_continues_validation(self):
        validator, client = make_validator(True)

        assert validator.validate_pr(7378) is True

        client.has_human_reopened.assert_called_once_with(7378)
        client.close_pr.assert_not_called()
        posted = [c.args[1] for c in client.post_comment.call_args_list]
        assert not any(SPAM_CLOSE_COMMENT in body for body in posted)
        client.add_labels.assert_any_call(7378, LABEL_PR_CHECK_BLOCKED)
        assert any(REVIEW_COMMENT_PARTIAL in body for body in posted)
        client.fetch_review_state.assert_called_once_with(7378)

    def test_lookup_failure_skips_close_and_continues_validation(self):
        validator, client = make_validator(None)

        assert validator.validate_pr(7378) is True

        client.has_human_reopened.assert_called_once_with(7378)
        client.close_pr.assert_not_called()
        posted = [c.args[1] for c in client.post_comment.call_args_list]
        assert not any(SPAM_CLOSE_COMMENT in body for body in posted)
        client.add_labels.assert_any_call(7378, LABEL_PR_CHECK_BLOCKED)
        client.fetch_review_state.assert_called_once_with(7378)

    def test_no_human_reopen_still_closes(self):
        validator, client = make_validator(False)

        assert validator.validate_pr(7378) is True

        client.has_human_reopened.assert_called_once_with(7378)
        client.close_pr.assert_called_once_with(7378)
        posted = client.post_comment.call_args.args[1]
        assert SPAM_CLOSE_COMMENT in posted
        client.fetch_review_state.assert_not_called()

    def test_reopen_lookup_skipped_when_not_closure_eligible(self):
        validator, client = make_validator(
            True, raw_pr=make_spam_pr(created_at=CUTOFF_INELIGIBLE)
        )

        assert validator.validate_pr(7378) is True

        client.has_human_reopened.assert_not_called()
        client.close_pr.assert_not_called()

    def test_reopen_lookup_skipped_when_leave_open(self):
        validator, client = make_validator(
            True, raw_pr=make_spam_pr(labels=[LABEL_LEAVE_OPEN])
        )

        assert validator.validate_pr(7378) is True

        client.has_human_reopened.assert_not_called()
        client.close_pr.assert_not_called()
