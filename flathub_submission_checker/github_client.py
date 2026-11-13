import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from github import Auth, Github
from github.GithubException import GithubException

from flathub_submission_checker.constants import (
    BOT_LOGINS,
    LABEL_STALE,
    LABEL_TOO_NEW,
)
from flathub_submission_checker.models import RawPullRequest

logger = logging.getLogger(__name__)

GITHUB_CALL_EXCEPTIONS = GithubException

ISSUE_EVENTS_PER_PAGE = 100
BOT_ACTOR_TYPE = "bot"
HUMAN_ACTOR_TYPE = "user"
BOT_LOGIN_SUFFIX = "[bot]"
BOT_COMMAND_PREFIX = "bot,"


@dataclass(frozen=True)
class ReviewState:
    unresolved_threads: int
    changes_pending: bool


def is_bot_command(body: Any) -> bool:
    if not isinstance(body, str):
        return False
    return body.lstrip().lower().startswith(BOT_COMMAND_PREFIX)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def has_pending_changes(pull_request: dict[str, Any]) -> bool | None:
    author = pull_request.get("author") or {}
    author_login = author.get("login")
    head_oid = pull_request.get("headRefOid")
    if not isinstance(head_oid, str) or not head_oid:
        return None
    comments = (pull_request.get("comments") or {}).get("nodes") or []
    reviews = (pull_request.get("reviews") or {}).get("nodes") or []
    if not isinstance(reviews, list) or not isinstance(comments, list):
        return None

    latest_by_reviewer: dict[str, dict[str, Any]] = {}
    for review in reviews:
        if not isinstance(review, dict):
            return None
        reviewer = (review.get("author") or {}).get("login")
        if not isinstance(reviewer, str) or not reviewer:
            continue
        if reviewer == author_login or reviewer in BOT_LOGINS:
            continue
        if reviewer.endswith(BOT_LOGIN_SUFFIX):
            continue
        if "submittedAt" not in review:
            return None
        submitted_at = parse_timestamp(review.get("submittedAt"))
        if submitted_at is None:
            return None
        previous = latest_by_reviewer.get(reviewer)
        if previous is None or submitted_at > previous["submitted_at"]:
            latest_by_reviewer[reviewer] = {
                "state": review.get("state"),
                "submitted_at": submitted_at,
                "oid": (review.get("commit") or {}).get("oid"),
            }

    for review in latest_by_reviewer.values():
        if review["state"] != "CHANGES_REQUESTED":
            continue
        if review["oid"] is not None and review["oid"] != head_oid:
            continue
        replied = _author_replied_after(comments, author_login, review["submitted_at"])
        if replied is None:
            return None
        if replied:
            continue
        return True
    return False


def _author_replied_after(
    comments: list[dict[str, Any]], author_login: str | None, since: datetime
) -> bool | None:
    if not author_login:
        return False
    for comment in comments:
        if not isinstance(comment, dict):
            return None
        if (comment.get("author") or {}).get("login") != author_login:
            continue
        if "createdAt" not in comment:
            return None
        created_at = parse_timestamp(comment.get("createdAt"))
        if created_at is None:
            return None
        if created_at <= since:
            continue
        if is_bot_command(comment.get("body")):
            continue
        return True
    return False


def is_bot_actor(actor: dict[str, Any] | None) -> bool | None:
    if not actor:
        return None
    login = actor.get("login")
    if not isinstance(login, str) or not login:
        return None
    if login in BOT_LOGINS or login.endswith(BOT_LOGIN_SUFFIX):
        return True
    actor_type = actor.get("type")
    if not isinstance(actor_type, str) or not actor_type:
        return None
    actor_type = actor_type.lower()
    if actor_type == BOT_ACTOR_TYPE:
        return True
    if actor_type == HUMAN_ACTOR_TYPE:
        return False
    return None


class GitHubClient(Protocol):
    def add_labels(self, pr_number: int, *labels: str) -> bool: ...
    def remove_labels(self, pr_number: int, *labels: str) -> bool: ...
    def post_comment(self, pr_number: int, body: str) -> bool: ...
    def close_pr(self, pr_number: int) -> bool: ...
    def fetch_pr_numbers(
        self,
        is_draft: bool,
        created_after: datetime,
        updated_after: datetime,
        scan_limit: int,
        result_limit: int,
    ) -> list[int] | None: ...
    def fetch_pull_request(self, pr_number: int) -> RawPullRequest | None: ...
    def fetch_review_state(self, pr_number: int) -> ReviewState | None: ...
    def has_human_reopened(self, pr_number: int) -> bool | None: ...


class PyGithubClient:
    def __init__(self, gh_token: str, gh_repo: str) -> None:
        self.gh_repo = gh_repo
        self.owner_login, self.repo_name = gh_repo.split("/", 1)
        self.gh = Github(auth=Auth.Token(gh_token))
        self.repo = self.gh.get_repo(gh_repo)

    def add_labels(self, pr_number: int, *labels: str) -> bool:
        try:
            pr = self.repo.get_pull(pr_number)
            existing = {label.name for label in pr.get_labels()}
            if LABEL_TOO_NEW in existing:
                return True
            for label in labels:
                if label not in existing:
                    pr.add_to_labels(label)
                    logger.info("Added label %s to PR #%s", label, pr_number)
            return True
        except GITHUB_CALL_EXCEPTIONS as err:
            logger.error("Failed to add labels %r on PR %s: %s", labels, pr_number, err)
            return False

    def remove_labels(self, pr_number: int, *labels: str) -> bool:
        try:
            pr = self.repo.get_pull(pr_number)
            existing = {label.name for label in pr.get_labels()}
            for label in labels:
                if label in existing:
                    pr.remove_from_labels(label)
                    logger.info("Removed label %s from PR #%s", label, pr_number)
            return True
        except GITHUB_CALL_EXCEPTIONS as err:
            logger.error(
                "Failed to remove labels %r on PR %s: %s", labels, pr_number, err
            )
            return False

    def post_comment(self, pr_number: int, body: str) -> bool:
        try:
            pr = self.repo.get_pull(pr_number)
            pr.create_issue_comment(body)
            return True
        except GITHUB_CALL_EXCEPTIONS as err:
            logger.error("Failed to post comment on PR %s: %s", pr_number, err)
            return False

    def close_pr(self, pr_number: int) -> bool:
        try:
            pr = self.repo.get_pull(pr_number)
            pr.edit(state="closed")
            return True
        except GITHUB_CALL_EXCEPTIONS as err:
            logger.error("Failed to close PR %s: %s", pr_number, err)
            return False

    def fetch_pr_numbers(
        self,
        is_draft: bool,
        created_after: datetime,
        updated_after: datetime,
        scan_limit: int,
        result_limit: int,
    ) -> list[int] | None:
        try:
            matched: list[int] = []
            pulls = self.repo.get_pulls(
                state="open", base="new-pr", sort="created", direction="desc"
            )

            for scanned, pr in enumerate(pulls):
                if scanned >= scan_limit or len(matched) >= result_limit:
                    break
                if pr.draft != is_draft:
                    continue
                if pr.created_at is None or pr.created_at < created_after:
                    continue
                if pr.updated_at is None or pr.updated_at < updated_after:
                    continue
                if LABEL_STALE in {lbl.name for lbl in pr.get_labels()}:
                    continue
                matched.append(pr.number)

            logger.info(
                "Fetched %s %s PR(s): %s",
                len(matched),
                "draft" if is_draft else "non-draft",
                matched,
            )
            return matched
        except GITHUB_CALL_EXCEPTIONS as err:
            logger.error("Failed to fetch PR list (is_draft=%s): %s", is_draft, err)
            return None

    def fetch_pull_request(self, pr_number: int) -> RawPullRequest | None:
        try:
            pr = self.repo.get_pull(pr_number)
            return cast(RawPullRequest, pr)
        except GITHUB_CALL_EXCEPTIONS as err:
            logger.error("Failed to fetch PR %s: %s", pr_number, err)
            return None

    def fetch_review_state(self, pr_number: int) -> ReviewState | None:
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              headRefOid
              author {
                login
              }
              reviewThreads(first: 100) {
                nodes {
                  isResolved
                }
              }
              reviews(states: [CHANGES_REQUESTED, APPROVED, DISMISSED], last: 100) {
                nodes {
                  state
                  submittedAt
                  author {
                    login
                  }
                  commit {
                    oid
                  }
                }
              }
              comments(last: 50) {
                nodes {
                  createdAt
                  body
                  author {
                    login
                  }
                }
              }
            }
          }
        }
        """
        variables = {
            "owner": self.owner_login,
            "repo": self.repo_name,
            "number": pr_number,
        }
        try:
            _, data = self.gh.requester.requestJsonAndCheck(
                "POST", "/graphql", input={"query": query, "variables": variables}
            )
        except GITHUB_CALL_EXCEPTIONS as err:
            logger.error("Failed to fetch review state for PR %s: %s", pr_number, err)
            return None

        try:
            pull_request = data["data"]["repository"]["pullRequest"]
            nodes = pull_request["reviewThreads"]["nodes"]
            count = sum(1 for n in nodes if n["isResolved"] is False)
            changes_pending = has_pending_changes(pull_request)
            if changes_pending is None:
                logger.error("Could not determine change requests on PR %s", pr_number)
                return None
        except (KeyError, TypeError, AttributeError) as err:
            logger.error(
                "Unexpected review-state response on PR %s: %s",
                pr_number,
                err,
            )
            return None

        logger.info(
            "PR #%s has unresolved threads: %s, changes pending: %s",
            pr_number,
            count,
            changes_pending,
        )
        return ReviewState(
            unresolved_threads=count,
            changes_pending=changes_pending,
        )

    def has_human_reopened(self, pr_number: int) -> bool | None:
        unknown_actor = False
        page = 1
        try:
            while True:
                _, events = self.gh.requester.requestJsonAndCheck(
                    "GET",
                    f"/repos/{self.owner_login}/{self.repo_name}"
                    f"/issues/{pr_number}/events",
                    parameters={
                        "per_page": ISSUE_EVENTS_PER_PAGE,
                        "page": page,
                    },
                )
                if not isinstance(events, list):
                    logger.error(
                        "Unexpected issue-events response on PR %s: %r",
                        pr_number,
                        events,
                    )
                    return None
                for event in events:
                    if not isinstance(event, dict):
                        unknown_actor = True
                        continue
                    if event.get("event") != "reopened":
                        continue
                    actor = event.get("actor")
                    actor_dict = actor if isinstance(actor, dict) else None
                    is_bot = is_bot_actor(actor_dict)
                    if is_bot is False:
                        logger.info(
                            "PR #%s was reopened by a human (%s)",
                            pr_number,
                            actor_dict.get("login") if actor_dict else None,
                        )
                        return True
                    if is_bot is None:
                        unknown_actor = True
                if len(events) < ISSUE_EVENTS_PER_PAGE:
                    break
                page += 1
        except GITHUB_CALL_EXCEPTIONS as err:
            logger.error("Failed to fetch issue events for PR %s: %s", pr_number, err)
            return None

        if unknown_actor:
            logger.info(
                "PR #%s has a reopen event from an unknown actor; failing safe",
                pr_number,
            )
            return None
        return False
