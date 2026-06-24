import logging
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol

from flathub_submission_checker.constants import (
    BOT_LOGINS,
    BUILD_STARTED_COMMENT_PARTIAL,
    BUILD_SUCCESS_COMMENT,
    MASTER_COMMIT_AUTHOR_EMAIL,
    MASTER_COMMIT_MESSAGE,
)

logger = logging.getLogger(__name__)


class RawComment(Protocol):
    body: str
    user: Any


class RawCommit(Protocol):
    commit: Any


class RawLabel(Protocol):
    name: str


class RawFile(Protocol):
    filename: str


class RawPullRequest(Protocol):
    number: int
    title: str | None
    body: str | None
    draft: bool

    def get_issue_comments(self) -> list[RawComment]: ...
    def get_files(self) -> list[RawFile]: ...
    def get_labels(self) -> list[RawLabel]: ...
    def get_commits(self) -> list[RawCommit]: ...


def extract_bot_comment_lines(pr: RawPullRequest) -> list[str]:
    bot_comments = "\n".join(
        comment.body
        for comment in pr.get_issue_comments()
        if comment.user and comment.user.login in BOT_LOGINS
    )
    return bot_comments.split("\n")


def has_master_commit(pr: RawPullRequest) -> bool:
    commits = list(pr.get_commits())
    if len(commits) <= 1:
        return False
    second_commit = commits[1].commit
    return bool(
        second_commit.author.email == MASTER_COMMIT_AUTHOR_EMAIL
        and second_commit.message == MASTER_COMMIT_MESSAGE
    )


class ValidationResult(NamedTuple):
    is_valid: bool
    reasons: list[str]
    domain: str | None = None


@dataclass
class PRContext:
    number: int
    title: str
    body: str
    is_draft: bool
    files: list[str]
    labels: set[str]
    comment_lines: list[str] = field(default_factory=list)
    has_master_commit: bool = False

    @classmethod
    def from_pull_request(cls, pr: RawPullRequest) -> "PRContext":
        ctx = cls(
            number=pr.number,
            title=pr.title or "",
            body=(pr.body or "").replace("\r", ""),
            is_draft=bool(pr.draft),
            files=[f.filename for f in pr.get_files()],
            labels={lbl.name for lbl in pr.get_labels()},
            comment_lines=extract_bot_comment_lines(pr),
            has_master_commit=has_master_commit(pr),
        )
        logger.info(
            "Found PR details: PR #%s title=%r draft=%s files=%s labels=%s",
            ctx.number,
            ctx.title,
            ctx.is_draft,
            len(ctx.files),
            sorted(ctx.labels),
        )
        return ctx

    def comment_exists(self, comment: str) -> bool:
        return any(comment in line for line in self.comment_lines)

    def comment_exists_any(self, *comments: str) -> bool:
        return any(self.comment_exists(c) for c in comments)

    def comment_contains(self, substr: str) -> bool:
        return any(substr in line for line in self.comment_lines)

    def has_any_label(self, *labels: str) -> bool:
        return any(label in self.labels for label in labels)

    def _latest_build_comment(self) -> str | None:
        build_lines = [
            line for line in self.comment_lines if "test build" in line.lower()
        ]
        return build_lines[-1] if build_lines else None

    def latest_build_succeeded(self) -> bool:
        last_build_comment = self._latest_build_comment()
        return (
            last_build_comment is not None
            and BUILD_SUCCESS_COMMENT in last_build_comment
        )

    def latest_build_ongoing(self) -> bool:
        last_build_comment = self._latest_build_comment()
        return (
            last_build_comment is not None
            and BUILD_STARTED_COMMENT_PARTIAL in last_build_comment
        )

    def record_comment(self, body: str) -> None:
        self.comment_lines.extend(body.split("\n"))
