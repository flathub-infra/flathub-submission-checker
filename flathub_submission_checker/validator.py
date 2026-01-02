import logging
from datetime import UTC, datetime, timedelta

from flathub_submission_checker.constants import (
    BUILD_START_COMMENT,
    BUILD_START_COMMENT_PARTIAL,
    BUILD_SUCCESS_COMMENT,
    LABEL_AWAITING_CHANGES,
    LABEL_AWAITING_REVIEW,
    LABEL_AWAITING_UPSTREAM,
    LABEL_BLOCKED,
    LABEL_LEAVE_OPEN,
    LABEL_PR_CHECK_BLOCKED,
    LABEL_REVIEWED_WAITING,
    LABEL_WORK_IN_PROGRESS,
    REVIEW_COMMENT_PARTIAL,
    SPAM_CLOSE_COMMENT,
)
from flathub_submission_checker.github_client import GitHubClient
from flathub_submission_checker.models import PRContext
from flathub_submission_checker.validation import (
    build_domain_comment,
    build_review_comment,
    is_considered_spam,
    validate_pr_structure,
)

logger = logging.getLogger(__name__)


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


def should_demote_to_awaiting_changes(ctx: PRContext, unresolved_threads: int) -> bool:
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


def should_promote_to_awaiting_review(ctx: PRContext, unresolved_threads: int) -> bool:
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


class PRValidator:
    CUTOFF_DATE = datetime(2025, 5, 25, tzinfo=UTC)
    PR_LIST_LIMIT = 30
    PR_LIST_SCAN_LIMIT = 50

    def __init__(self, client: GitHubClient, gh_repo: str) -> None:
        self.client = client
        self.gh_repo = gh_repo
        self.updated_since = datetime.now(UTC) - timedelta(days=2)

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

        spam_ret, spam_comment = is_considered_spam(ctx.files, ctx.body)
        if spam_ret:
            logger.info("PR #%s considered spam, closing", ctx.number)
            SPAM_COMMENT = f"{SPAM_CLOSE_COMMENT} {spam_comment}."
            ok = self._comment(ctx, SPAM_COMMENT)
            return self.client.close_pr(ctx.number) and ok

        ok = True
        if not ctx.is_draft:
            logger.info(
                "PR #%s is not a draft, removing work-in-progress label", ctx.number
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
                "Failed to fetch unresolved review threads on PR #%s", ctx.number
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
            logger.info("PR #%s is a draft, adding work-in-progress label", pr_num)
            ok = self.client.add_labels(pr_num, LABEL_WORK_IN_PROGRESS)
        return self.validate_pr(pr_num) and ok
