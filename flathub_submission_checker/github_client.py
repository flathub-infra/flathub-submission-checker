import logging
from datetime import datetime
from typing import Protocol, cast

from github import Auth, Github
from github.GithubException import GithubException

from flathub_submission_checker.constants import LABEL_STALE
from flathub_submission_checker.models import RawPullRequest

logger = logging.getLogger(__name__)

GITHUB_CALL_EXCEPTIONS = GithubException


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
    def count_unresolved_review_threads(self, pr_number: int) -> int | None: ...


class PyGithubClient:
    def __init__(self, gh_token: str, gh_repo: str) -> None:
        self.gh_repo = gh_repo
        self.owner_login, self.repo_name = gh_repo.split("/", 1)
        self.gh = Github(auth=Auth.Token(gh_token))
        self.repo = self.gh.get_repo(gh_repo)

    def add_labels(self, pr_number: int, *labels: str) -> bool:
        try:
            pr = self.repo.get_pull(pr_number)
            for label in labels:
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
                logger.info(
                    "Skipped removing label %s from PR #%s as it did not exist",
                    label,
                    pr_number,
                )
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

            logger.info("Fetched %s PR(s): %s", len(matched), matched)
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

    def count_unresolved_review_threads(self, pr_number: int) -> int | None:
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              reviewThreads(first: 100) {
                nodes {
                  isResolved
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
            logger.error("Failed to fetch review threads for PR %s: %s", pr_number, err)
            return None

        try:
            nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
            count = sum(1 for n in nodes if n["isResolved"] is False)
            logger.info("PR #%s has unresolved threads: %s", pr_number, count)
            return count
        except (KeyError, TypeError) as err:
            logger.error(
                "Unexpected review-threads response on PR %s: %s",
                pr_number,
                err,
            )
            return None
