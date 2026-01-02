#!/usr/bin/env python3

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final, NamedTuple, Protocol, cast

from github import Auth, Github
from github.GithubException import GithubException
from publicsuffixlist import PublicSuffixList  # type: ignore[import-untyped]

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

GITHUB_BASE_URL = "https://github.com"
FLATHUB_REPO_SLUG = "flathub/flathub"
FLATHUB_DOCS_BASE_URL = "https://docs.flathub.org/docs/for-app-authors"

REQUIREMENTS_URL = f"{FLATHUB_DOCS_BASE_URL}/requirements"
SUBMISSION_URL = f"{FLATHUB_DOCS_BASE_URL}/submission"
VERIFICATION_URL = f"{FLATHUB_DOCS_BASE_URL}/verification"
MASTER_BRANCH_URL = f"{GITHUB_BASE_URL}/{FLATHUB_REPO_SLUG}/commits/master/"
PR_TEMPLATE_URL: Final = (
    f"{GITHUB_BASE_URL}/{FLATHUB_REPO_SLUG}/blob/master/"
    ".github/pull_request_template.md?plain=1"
)

ADD_PREFIX_RE = re.compile(r"^add\s+", re.IGNORECASE)
APPID_COMPONENT_RE = re.compile(r"^[A-Za-z_][\w\-]*$")
FLATHUB_JSON_RE = re.compile(r".*/flathub\.json$")
TOPLEVEL_MANIFEST_RE = re.compile(r"^[^/]+\.(ya?ml|json)$")
CHECKLIST_LINE_RE = re.compile(r"^- \[([ xX])\]\s*(.+)$", re.MULTILINE)

VIDEO_CHECKLIST_ITEM = (
    "Please attach a video showcasing the application on Linux using the Flatpak."
)
VIDEO_LINK_RE = re.compile(r"https?://\S+")
VIDEO_NA_RE = re.compile(r"\b(n/?a|no\s+video(?:\s+available)?)\b", re.IGNORECASE)
VIDEO_LOOKAHEAD_LINES = 2

MASTER_COMMIT_AUTHOR_EMAIL = "mclasen@redhat.com"
MASTER_COMMIT_MESSAGE = "Add some instructions"

BOT_LOGINS = frozenset({"flathubbot", "github-actions", "github-actions[bot]"})

LABEL_WORK_IN_PROGRESS = "work-in-progress"
LABEL_PR_CHECK_BLOCKED = "pr-check-blocked"
LABEL_BLOCKED = "blocked"
LABEL_AWAITING_REVIEW = "awaiting-review"
LABEL_AWAITING_CHANGES = "awaiting-changes"
LABEL_AWAITING_UPSTREAM = "awaiting-upstream"
LABEL_REVIEWED_WAITING = "reviewed-waiting"
LABEL_STALE = "Stale"
LABEL_LEAVE_OPEN = "leave-open"

CHECKLIST_ITEMS = (
    "Please describe the application briefly.",
    "Please attach a video showcasing the application on Linux using the Flatpak.",
    "The Flatpak ID follows all the rules listed in the",
    "I have read and followed all the",
)

ROLE_CHECKLIST_RE = re.compile(
    r"I am (?:an?|the)\s+(?:author|developer|contributor|upstream contributor)\b"
    r"|I contacted upstream",
    re.IGNORECASE,
)


def _role_checklist_matches(text: str) -> bool:
    return bool(ROLE_CHECKLIST_RE.search(text)) and "the project" in text.lower()


EXCLUDED_ID_PREFIXES = (
    "com.github.",
    "com.gitlab.",
    "io.github.",
    "io.gitlab.",
    "org.gnome.gitlab.",
    "org.gnome.World.",
    "org.gnome.design",
    "org.kde.",
    "org.gnome.",
)

RUNTIME_PREFIXES = (
    "org.freedesktop.Platform.",
    "org.freedesktop.Sdk.",
    "org.gnome.Platform.",
    "org.gnome.Sdk.",
    "org.gtk.Gtk3theme.",
    "org.kde.KStyle.",
    "org.kde.Platform.",
    "org.kde.PlatformInputContexts.",
    "org.kde.PlatformTheme.",
    "org.kde.Sdk.",
    "org.kde.WaylandDecoration.",
    "org.freedesktop.LinuxAudio.",
)

CODE_HOST_PREFIXES = (
    "io.frama.",
    "page.codeberg.",
    "io.sourceforge.",
    "net.sourceforge.",
)

ADDON_COMPONENTS: Final = frozenset(
    {"addon", "addons", "extension", "extensions", "plugin", "plugins"}
)


def demangle(name: str) -> str:
    return name.removeprefix("_").replace("_", "-")


def get_domain(appid: str) -> str | None:
    if appid.count(".") < 2:
        logger.info("Flatpak ID has invalid number of components: %s", appid)
        return None

    if appid.startswith(EXCLUDED_ID_PREFIXES):
        logger.info(
            "Flatpak ID is excluded as it is in EXCLUDED_ID_PREFIXES: %s", appid
        )
        return None

    if appid.endswith(".BaseApp"):
        logger.info("Flatpak ID is excluded for BaseApps: %s", appid)
        return None

    if appid.split(".")[-2].lower() in ADDON_COMPONENTS:
        logger.info("Flatpak ID is excluded as it is in ADDON_COMPONENTS: %s", appid)
        return None

    if appid.startswith(RUNTIME_PREFIXES):
        logger.info("Flatpak ID is excluded as it is in RUNTIME_PREFIXES: %s", appid)
        return None

    if appid.startswith(CODE_HOST_PREFIXES):
        tld, host, name = appid.split(".")[:3]
        name = demangle(name)
        if host == "sourceforge":
            domain = f"{name}.{host}.io".lower()
        else:
            domain = f"{name}.{host}.{tld}".lower()
        logger.info(
            "Derived the code host domain %s from the Flatpak ID %s", domain, appid
        )
        return domain

    fqdn = ".".join(reversed(appid.split("."))).lower()
    psl = PublicSuffixList()
    if psl.is_private(fqdn):
        priv = psl.privatesuffix(fqdn)
        if priv:
            domain = demangle(priv)
            logger.info(
                "Derived the PSL domain %s from the Flatpak ID %s", domain, appid
            )
            return domain

    parts = [demangle(p) for p in appid.split(".")[:-1]]
    domain = ".".join(reversed(parts)).lower()
    logger.info("Derived the fallback domain %s from the Flatpak ID %s", domain, appid)
    return domain


def get_appid_from_pr_title(title: str) -> str | None:
    matched = ADD_PREFIX_RE.match(title)
    if not matched:
        logger.info("PR title does not match ADD_PREFIX_RE: %s", title)
        return None

    appid = title[matched.end() :].strip()
    parts = appid.split(".")

    if not (3 <= len(parts) <= 255):
        logger.info("Flatpak ID has invalid number of parts: %s", appid)
        return None

    if not all(APPID_COMPONENT_RE.match(p) for p in parts):
        logger.info("Flatpak ID has invalid component syntax: %s", appid)
        return None

    logger.info("Extracted Flatpak ID %s from PR title %s", appid, title)
    return appid


def parse_checklist(body: str) -> list[tuple[bool, str]]:
    checklist = [
        (mark.lower() == "x", text.strip())
        for mark, text in CHECKLIST_LINE_RE.findall(body)
    ]
    logger.info("Found %s checklist line(s)", len(checklist))

    unchecked = [text for checked, text in checklist if not checked]
    if unchecked:
        logger.info("Found unchecked line(s): %s", unchecked)

    return checklist


def _checklist_item_matches(text: str) -> bool:
    return any(item in text for item in CHECKLIST_ITEMS) or _role_checklist_matches(
        text
    )


REQUIRED_CHECKLIST_COUNT = len(CHECKLIST_ITEMS) + 1
MAX_UNCHECKED_ITEMS_ALLOWED = 1


def checklist_matches_template(checklist: list[tuple[bool, str]]) -> bool:
    texts = [text for _, text in checklist]

    missing_items = [
        item for item in CHECKLIST_ITEMS if not any(item in text for text in texts)
    ]

    role_matches = any(_role_checklist_matches(text) for text in texts)
    if not role_matches:
        missing_items.append("Role item: author/developer/contributor")

    matches = len(missing_items) <= MAX_UNCHECKED_ITEMS_ALLOWED

    if missing_items:
        logger.info("Found missing required item(s): %s", missing_items)

    return matches


def checklist_fully_checked(checklist: list[tuple[bool, str]]) -> bool:
    if not checklist:
        logger.info("Checklist is empty, not fully checked")
        return False

    relevant = [checked for checked, text in checklist if _checklist_item_matches(text)]
    if len(relevant) < REQUIRED_CHECKLIST_COUNT:
        logger.info(
            "Checklist contains only %s/%s required items",
            len(relevant),
            REQUIRED_CHECKLIST_COUNT,
        )
        return False
    return all(relevant)


def count_unchecked_relevant_items(checklist: list[tuple[bool, str]]) -> int:
    relevant = [checked for checked, text in checklist if _checklist_item_matches(text)]
    unchecked_count = sum(1 for checked in relevant if not checked)
    logger.info(
        "Found %s relevant checklists and %s relevant but unchecked checklists",
        len(relevant),
        unchecked_count,
    )
    return unchecked_count


def has_missing_video(body: str) -> bool:
    checklist = parse_checklist(body)
    video_checked = any(
        checked for checked, text in checklist if VIDEO_CHECKLIST_ITEM in text
    )
    if not video_checked:
        logger.info("Video checklist item is unchecked or missing")
        return True

    lines = body.split("\n")

    for i, line in enumerate(lines):
        if VIDEO_CHECKLIST_ITEM not in line:
            continue

        after_item = line.split(VIDEO_CHECKLIST_ITEM, 1)[1]
        lookahead_lines = []
        for offset in range(1, VIDEO_LOOKAHEAD_LINES + 1):
            j = i + offset
            if j >= len(lines) or CHECKLIST_LINE_RE.match(lines[j]):
                break
            lookahead_lines.append(lines[j])

        search_text = "\n".join([after_item, *lookahead_lines])

        if VIDEO_NA_RE.search(search_text):
            logger.info("Video checklist item marked N/A or no video available")
            return True

        if VIDEO_LINK_RE.search(search_text):
            return False

        logger.info(
            "Video checklist item has no link within %s line(s) after it",
            VIDEO_LOOKAHEAD_LINES,
        )
        return True

    logger.info("Video checklist item not found in PR body")
    return True


def is_considered_spam(files: list[str], body: str) -> bool:

    if files and all("/" in f for f in files):
        logger.info(
            "All files are nested in subdirectories, flagging as spam: %s", files
        )
        return True

    checklist = parse_checklist(body)

    if not checklist_matches_template(checklist):
        logger.info("Checklist missing or altered, flagging as spam")
        return True

    if has_missing_video(body):
        logger.info(
            "Video checklist item missing, unchecked, or has no link, flagging as spam"
        )
        return True

    unchecked_count = count_unchecked_relevant_items(checklist)
    result = unchecked_count > MAX_UNCHECKED_ITEMS_ALLOWED
    logger.info(
        "Unchecked checklist count is %s > %s",
        unchecked_count,
        MAX_UNCHECKED_ITEMS_ALLOWED,
    )
    return result


def is_review_command(text: str) -> bool:
    return text.strip().lower() == "/review"


class ValidationResult(NamedTuple):
    is_valid: bool
    reasons: list[str]
    domain: str | None = None


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
    def from_pull_request(cls, pr: RawPullRequest) -> PRContext:
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

    def comment_exists_any(self, *comment: str) -> bool:
        return any(self.comment_exists(comment) for comment in comment)

    def comment_contains(self, substr: str) -> bool:
        return any(substr in line for line in self.comment_lines)

    def has_any_label(self, *labels: str) -> bool:
        return any(label in self.labels for label in labels)

    def latest_build_succeeded(self, build_success_comment: str) -> bool:
        build_lines = [line for line in self.comment_lines if "Test build" in line]
        return bool(build_lines) and bool(build_success_comment in build_lines[-1])

    def record_comment(self, body: str) -> None:
        self.comment_lines.extend(body.split("\n"))


BUILD_SUCCESS_COMMENT = "[Test build succeeded]"
BUILD_START_COMMENT = (
    "Starting a test build of the submission. Please fix any\n"
    "issues reported in the build log. You can restart the build\n"
    "once the issue is fixed by commenting the phrase below.\n\n"
    "bot, build"
)
BUILD_START_COMMENT_PARTIAL = "Starting a test build of the submission"
BASE_REVIEW_COMMENT = (
    "This pull request is temporarily marked as blocked as some\n"
    "automated checks failed on it. Please make sure the\n"
    "following items are done:"
)
REVIEW_COMMENT_PARTIAL = "This pull request is temporarily marked as blocked as some"
DOMAIN_COMMENT_PARTIAL = "The domain to be used for verification is"
SPAM_CLOSE_COMMENT = (
    "This pull request does not follow the submission guidelines "
    "and has been closed automatically."
)


def build_review_comment(reasons: list[str]) -> str:
    lines = [BASE_REVIEW_COMMENT, *reasons]
    lines.append(
        f"- The [requirements]({REQUIREMENTS_URL}) "
        f"and [submission process]({SUBMISSION_URL}) "
        "have been followed"
    )
    return "\n".join(lines)


def build_domain_comment(domain: str) -> str:
    verif_url = f"https://{domain}/.well-known/org.flathub.VerifiedApps.txt"
    verif_comment = (
        f"If you intend to [verify]({VERIFICATION_URL}) "
        "this submission, please "
        "confirm by uploading an empty `org.flathub.VerifiedApps.txt` "
        f"file to {verif_url}. Otherwise, ignore this"
    )
    return (
        f"{DOMAIN_COMMENT_PARTIAL} {domain}. {verif_comment}. "
        "Please comment if this incorrect."
    )


def validate_pr_structure(ctx: PRContext) -> ValidationResult:
    appid = get_appid_from_pr_title(ctx.title)
    checklist = parse_checklist(ctx.body)

    checks: list[tuple[bool, str]] = [
        (appid is not None, '- PR title is "Add $FLATPAK_ID"'),
        (
            not ctx.has_master_commit,
            "- PR does not contain commits from the "
            f"[master branch]({MASTER_BRANCH_URL})",
        ),
        (
            not any(FLATHUB_JSON_RE.match(f) for f in ctx.files),
            "- flathub.json file is at toplevel",
        ),
        (
            any(TOPLEVEL_MANIFEST_RE.match(f) for f in ctx.files),
            "- Flatpak manifest is at toplevel",
        ),
        (
            checklist_fully_checked(checklist),
            f"- All [checklists]({PR_TEMPLATE_URL}) "
            "are present in PR body and are completed",
        ),
    ]

    reasons = [message for passed, message in checks if not passed]
    domain = get_domain(appid) if appid else None

    return ValidationResult(is_valid=not reasons, reasons=reasons, domain=domain)


def should_start_build(ctx: PRContext) -> bool:
    already_building_or_built = ctx.comment_exists_any(
        BUILD_START_COMMENT_PARTIAL
    ) or ctx.latest_build_succeeded(BUILD_SUCCESS_COMMENT)
    is_blocked = ctx.has_any_label(LABEL_PR_CHECK_BLOCKED, LABEL_BLOCKED)

    result = not is_blocked and not already_building_or_built

    if not result:
        if is_blocked:
            logger.info("Not starting build: PR #%s is blocked", ctx.number)
        if already_building_or_built:
            logger.info(
                "Not starting build: PR #%s already building or built", ctx.number
            )

    return result


def should_post_domain_comment(ctx: PRContext, domain: str | None) -> bool:
    if not domain:
        logger.info("Skipped domain comment on PR %s: %s", ctx.number, domain)
        return False
    if ctx.has_any_label(LABEL_BLOCKED):
        logger.info(
            "Skipped domain comment on PR %s as it has LABEL_BLOCKED", ctx.number
        )
        return False
    verif_url = f"https://{domain}/.well-known/org.flathub.VerifiedApps.txt"
    return not ctx.comment_contains(verif_url)


def should_mark_awaiting_review(ctx: PRContext) -> bool:
    blocking_label = ctx.has_any_label(
        LABEL_AWAITING_CHANGES,
        LABEL_AWAITING_UPSTREAM,
        LABEL_BLOCKED,
        LABEL_REVIEWED_WAITING,
    )
    result = not blocking_label

    if not result:
        logger.info(
            "Not marking PR #%s as awaiting-review: already has a conflicting label",
            ctx.number,
        )

    return result


def should_demote_to_awaiting_changes(
    ctx: PRContext,
    unresolved_threads: int,
) -> bool:
    has_awaiting_review = ctx.has_any_label(LABEL_AWAITING_REVIEW)
    result = has_awaiting_review and unresolved_threads > 0

    if not result:
        if not has_awaiting_review:
            logger.info(
                "Not demoting PR #%s to awaiting-changes: "
                "not currently awaiting-review",
                ctx.number,
            )
        elif unresolved_threads <= 0:
            logger.info(
                "Not demoting PR #%s to awaiting-changes: no unresolved review threads",
                ctx.number,
            )

    return result


def should_promote_to_awaiting_review(
    ctx: PRContext,
    unresolved_threads: int,
) -> bool:
    has_awaiting_changes = ctx.has_any_label(LABEL_AWAITING_CHANGES)
    has_blocking_label = ctx.has_any_label(
        LABEL_AWAITING_UPSTREAM,
        LABEL_WORK_IN_PROGRESS,
        LABEL_PR_CHECK_BLOCKED,
        LABEL_BLOCKED,
    )
    build_succeeded = ctx.latest_build_succeeded(BUILD_SUCCESS_COMMENT)

    result = (
        has_awaiting_changes
        and not has_blocking_label
        and build_succeeded
        and unresolved_threads == 0
    )

    if not result:
        if not has_awaiting_changes:
            logger.info(
                "Not promoting PR #%s to awaiting-review: "
                "not currently awaiting-changes",
                ctx.number,
            )
        if has_blocking_label:
            logger.info(
                "Not promoting PR #%s to awaiting-review: has a conflicting label",
                ctx.number,
            )
        if not build_succeeded:
            logger.info(
                "Not promoting PR #%s to awaiting-review: "
                "latest build hasn't succeeded",
                ctx.number,
            )
        if unresolved_threads > 0:
            logger.info(
                "Not promoting PR #%s to awaiting-review: "
                "%s unresolved review thread(s)",
                ctx.number,
                unresolved_threads,
            )

    return result


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


class PRValidator:
    CUTOFF_DATE: Final = datetime(2025, 5, 25, tzinfo=timezone.utc)
    PR_LIST_LIMIT: Final = 30
    PR_LIST_SCAN_LIMIT: Final = 50

    def __init__(self, client: GitHubClient, gh_repo: str) -> None:
        self.client = client
        self.gh_repo = gh_repo
        self.updated_since = datetime.now(timezone.utc) - timedelta(days=2)

    def fetch_filtered_prs(self, is_draft: bool) -> list[int] | None:
        matched = self.client.fetch_pr_numbers(
            is_draft=is_draft,
            created_after=self.CUTOFF_DATE,
            updated_after=self.updated_since,
            scan_limit=self.PR_LIST_SCAN_LIMIT,
            result_limit=self.PR_LIST_LIMIT,
        )
        if matched is None:
            logger.info("Failed to match any filtered PRs")
            return None
        logger.info(
            "Found %s %s PRs matching criteria",
            len(matched),
            "draft" if is_draft else "non-draft",
        )
        return matched

    def _comment(self, ctx: PRContext, body: str) -> bool:
        if ctx.comment_exists_any(body):
            logger.info("Comment already exists on PR #%s, skipping", ctx.number)
            return True
        if not self.client.post_comment(ctx.number, body):
            logger.info("Failed to comment on PR #%s", ctx.number)
            return False
        ctx.record_comment(body)
        return True

    def label_draft_prs(self, draft_pr_numbers: list[int]) -> None:
        for pr_num in draft_pr_numbers:
            logger.info("Adding draft label to PR #%s", pr_num)
            self.client.add_labels(pr_num, LABEL_WORK_IN_PROGRESS)

    def start_build_if_needed(self, ctx: PRContext) -> bool:
        if should_start_build(ctx):
            logger.info("Starting build on PR #%s", ctx.number)
            return self._comment(ctx, BUILD_START_COMMENT)
        return True

    def post_domain_comment_if_needed(self, ctx: PRContext, domain: str | None) -> bool:
        if domain and should_post_domain_comment(ctx, domain):
            return self._comment(ctx, build_domain_comment(domain))
        return True

    def process_unblocked_pr(self, ctx: PRContext, domain: str | None) -> bool:
        ok = self.post_domain_comment_if_needed(ctx, domain)
        ok = self.client.remove_labels(ctx.number, LABEL_PR_CHECK_BLOCKED) and ok

        if should_mark_awaiting_review(ctx):
            logger.info("Marking PR #%s as awaiting-review", ctx.number)
            ok = self.client.add_labels(ctx.number, LABEL_AWAITING_REVIEW) and ok

        return self.start_build_if_needed(ctx) and ok

    def process_blocked_pr(self, ctx: PRContext, reasons: list[str]) -> bool:
        ok = self.client.add_labels(ctx.number, LABEL_PR_CHECK_BLOCKED)
        ok = self.client.remove_labels(ctx.number, LABEL_AWAITING_REVIEW) and ok

        if not ctx.comment_exists(REVIEW_COMMENT_PARTIAL):
            logger.info("Posting review comment on PR #%s", ctx.number)
            ok = self._comment(ctx, build_review_comment(reasons)) and ok

        return ok

    def update_review_state(self, ctx: PRContext, unresolved_threads: int) -> bool:
        if should_demote_to_awaiting_changes(ctx, unresolved_threads):
            ok = self.client.add_labels(ctx.number, LABEL_AWAITING_CHANGES)
            return self.client.remove_labels(ctx.number, LABEL_AWAITING_REVIEW) and ok
        if should_promote_to_awaiting_review(ctx, unresolved_threads):
            ok = self.client.add_labels(ctx.number, LABEL_AWAITING_REVIEW)
            return self.client.remove_labels(ctx.number, LABEL_AWAITING_CHANGES) and ok
        return True

    def validate_pr(self, pr_num: int) -> bool:
        raw_pr = self.client.fetch_pull_request(pr_num)
        if raw_pr is None:
            logger.info("PR #%s could not be fetched", pr_num)
            return False

        ctx = PRContext.from_pull_request(raw_pr)

        if ctx.has_any_label(LABEL_LEAVE_OPEN):
            logger.info("PR #%s has leave-open label, skipping", ctx.number)
            return True

        if is_considered_spam(ctx.files, ctx.body):
            logger.info("PR #%s considered spam, closing", ctx.number)
            ok = self._comment(ctx, SPAM_CLOSE_COMMENT)
            return self.client.close_pr(ctx.number) and ok

        ok = True
        if not ctx.is_draft:
            logger.info(
                "PR #%s is not a draft, removing work-in-progress label",
                ctx.number,
            )
            ok = self.client.remove_labels(ctx.number, LABEL_WORK_IN_PROGRESS) and ok

        validation = validate_pr_structure(ctx)
        if validation.is_valid:
            logger.info("PR #%s passed structure validation", ctx.number)
            ok = self.process_unblocked_pr(ctx, validation.domain) and ok
        else:
            logger.info(
                "PR #%s failed structure validation: %s",
                ctx.number,
                validation.reasons,
            )
            ok = self.process_blocked_pr(ctx, validation.reasons) and ok

        unresolved_threads = self.client.count_unresolved_review_threads(pr_num)
        if unresolved_threads is None:
            logger.info(
                "Failed to fetch unresolved review threads on PR #%s",
                ctx.number,
            )
            return False

        return self.update_review_state(ctx, unresolved_threads) and ok

    def run(self) -> bool:
        pr_numbers = self.fetch_filtered_prs(is_draft=False)
        draft_pr_numbers = self.fetch_filtered_prs(is_draft=True)

        if pr_numbers is None or draft_pr_numbers is None:
            logger.info("Failed to fetch PR list(s)")
            return False

        self.label_draft_prs(draft_pr_numbers)

        ok = True
        for pr_num in pr_numbers:
            logger.info("Validating PR #%s", pr_num)
            ok = self.validate_pr(pr_num) and ok
        return ok

    def run_single(self, pr_num: int) -> bool:
        raw_pr = self.client.fetch_pull_request(pr_num)
        if raw_pr is None:
            logger.info("PR #%s could not be fetched", pr_num)
            return False
        ok = True
        if raw_pr.draft:
            logger.info(
                "PR #%s is a draft, adding work-in-progress label",
                pr_num,
            )
            ok = self.client.add_labels(pr_num, LABEL_WORK_IN_PROGRESS)
        return self.validate_pr(pr_num) and ok


def main() -> int:
    try:
        gh_token = os.environ["GH_TOKEN"]
        gh_repo = os.environ["GH_REPO"]
    except KeyError as err:
        logger.error("Missing GH_TOKEN or GH_REPO: %s", err)
        return 1

    try:
        client = PyGithubClient(gh_token, gh_repo)
    except GITHUB_CALL_EXCEPTIONS as err:
        logger.error("Failed to initialise GitHub for %r: %s", gh_repo, err)
        return 1

    validator = PRValidator(client, gh_repo)

    comment_body = os.environ.get("COMMENT_BODY")
    if comment_body is not None:
        if not is_review_command(comment_body):
            return 0
        try:
            pr_num = int(os.environ["PR_NUMBER"])
        except (KeyError, ValueError) as err:
            logger.error("Missing or invalid PR_NUMBER: %s", err)
            return 1
        return 0 if validator.run_single(pr_num) else 1

    if len(sys.argv) > 1:
        try:
            pr_num = int(sys.argv[1])
        except ValueError as err:
            logger.error("Invalid PR number argument: %r: %s", sys.argv[1], err)
            return 1
        return 0 if validator.run_single(pr_num) else 1

    return 0 if validator.run() else 1


if __name__ == "__main__":
    raise SystemExit(main())
