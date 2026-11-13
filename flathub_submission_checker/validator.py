import logging
from datetime import UTC, datetime, timedelta

from flathub_submission_checker.constants import (
    BUILD_START_COMMENT,
    BUILD_START_COMMENT_PARTIAL,
    COMMENT_FOOTER,
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
from flathub_submission_checker.github_client import GitHubClient, ReviewState
from flathub_submission_checker.models import PRContext
from flathub_submission_checker.parsing import get_appid_from_pr_title, parse_checklist
from flathub_submission_checker.validation import (
    build_domain_comment,
    build_review_comment,
    is_considered_spam,
    validate_pr_structure,
)

logger = logging.getLogger(__name__)


def should_start_build(ctx: PRContext) -> bool:
    already_building_or_built = (
        ctx.comment_exists_any(BUILD_START_COMMENT_PARTIAL)
        or ctx.latest_build_ongoing()
        or ctx.latest_build_succeeded()
    )
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


def should_demote_to_awaiting_changes(ctx: PRContext, review: ReviewState) -> bool:
    has_awaiting_review = ctx.has_any_label(LABEL_AWAITING_REVIEW)
    result = has_awaiting_review and (
        review.unresolved_threads > 0 or review.changes_pending
    )

    if not result:
        if not has_awaiting_review:
            logger.info(
                "Not demoting PR #%s to awaiting-changes: "
                "not currently awaiting-review",
                ctx.number,
            )
        elif review.unresolved_threads <= 0 and not review.changes_pending:
            logger.info(
                "Not demoting PR #%s to awaiting-changes: no unresolved review "
                "threads or pending change requests",
                ctx.number,
            )

    return result


def should_promote_to_awaiting_review(ctx: PRContext, review: ReviewState) -> bool:
    has_awaiting_changes = ctx.has_any_label(LABEL_AWAITING_CHANGES)
    has_blocking_label = ctx.has_any_label(
        LABEL_AWAITING_UPSTREAM,
        LABEL_WORK_IN_PROGRESS,
        LABEL_PR_CHECK_BLOCKED,
        LABEL_BLOCKED,
    )
    build_succeeded = ctx.latest_build_succeeded()

    result = (
        has_awaiting_changes
        and not has_blocking_label
        and build_succeeded
        and review.unresolved_threads == 0
        and not review.changes_pending
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
        if review.unresolved_threads > 0:
            logger.info(
                "Not promoting PR #%s to awaiting-review: "
                "%s unresolved review thread(s)",
                ctx.number,
                review.unresolved_threads,
            )
        if review.changes_pending:
            logger.info(
                "Not promoting PR #%s to awaiting-review: change request still pending",
                ctx.number,
            )

    return result


class PRValidator:
    CUTOFF_DATE = datetime(2025, 5, 25, tzinfo=UTC)
    AUTO_CLOSE_CUTOFF_DATE = datetime(2026, 9, 9, tzinfo=UTC)
    PR_LIST_LIMIT = 30
    PR_LIST_SCAN_LIMIT = 75

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
        return matched

    def _comment(self, ctx: PRContext, body: str) -> bool:
        comment = f"{body}{COMMENT_FOOTER}"

        if ctx.comment_exists_any(body):
            logger.info("Comment already exists on PR #%s, skipping", ctx.number)
            return True

        if not self.client.post_comment(ctx.number, comment):
            logger.info("Failed to comment on PR #%s", ctx.number)
            return False

        ctx.record_comment(comment)
        return True

    def label_draft_prs(self, draft_pr_numbers: list[int]) -> None:
        for pr_num in draft_pr_numbers:
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
        if self.client.remove_labels(ctx.number, LABEL_PR_CHECK_BLOCKED):
            ctx.record_label_removed(LABEL_PR_CHECK_BLOCKED)
        else:
            ok = False

        if should_mark_awaiting_review(ctx):
            logger.info("Marking PR #%s as awaiting-review", ctx.number)
            if self.client.add_labels(ctx.number, LABEL_AWAITING_REVIEW):
                ctx.record_label_added(LABEL_AWAITING_REVIEW)
            else:
                ok = False

        return self.start_build_if_needed(ctx) and ok

    def process_blocked_pr(self, ctx: PRContext, reasons: list[str]) -> bool:
        ok = True
        if self.client.add_labels(ctx.number, LABEL_PR_CHECK_BLOCKED):
            ctx.record_label_added(LABEL_PR_CHECK_BLOCKED)
        else:
            ok = False
        if self.client.remove_labels(ctx.number, LABEL_AWAITING_REVIEW):
            ctx.record_label_removed(LABEL_AWAITING_REVIEW)
        else:
            ok = False

        if not ctx.comment_exists(REVIEW_COMMENT_PARTIAL):
            logger.info("Posting review comment on PR #%s", ctx.number)
            ok = self._comment(ctx, build_review_comment(reasons)) and ok

        return ok

    def update_review_state(self, ctx: PRContext, review: ReviewState) -> bool:
        if should_demote_to_awaiting_changes(ctx, review):
            ok = self.client.add_labels(ctx.number, LABEL_AWAITING_CHANGES)
            return self.client.remove_labels(ctx.number, LABEL_AWAITING_REVIEW) and ok
        if should_promote_to_awaiting_review(ctx, review):
            ok = self.client.add_labels(ctx.number, LABEL_AWAITING_REVIEW)
            return self.client.remove_labels(ctx.number, LABEL_AWAITING_CHANGES) and ok
        return True

    def _can_auto_close(self, ctx: PRContext) -> bool:
        human_reopened = self.client.has_human_reopened(ctx.number)
        if human_reopened is None:
            logger.info(
                "Could not determine reopen history for PR #%s; skipping auto-close",
                ctx.number,
            )
            return False
        if human_reopened:
            logger.info(
                "PR #%s was reopened by a human; skipping auto-close", ctx.number
            )
            return False
        return True

    def validate_pr(self, pr_num: int) -> bool:
        raw_pr = self.client.fetch_pull_request(pr_num)
        if raw_pr is None:
            logger.info("PR #%s could not be fetched", pr_num)
            return False

        ctx = PRContext.from_pull_request(raw_pr)
        appid = get_appid_from_pr_title(ctx.title)

        checklist = parse_checklist(ctx.body)

        spam_ret, spam_comments = is_considered_spam(
            checklist, ctx.files, ctx.body, ctx.labels, appid, ctx.created_at
        )
        if spam_ret and not ctx.has_any_label(LABEL_LEAVE_OPEN):
            if ctx.created_at is None or ctx.created_at < self.AUTO_CLOSE_CUTOFF_DATE:
                logger.info(
                    "PR #%s is not eligible for auto-close; skipping spam action",
                    ctx.number,
                )
                return True
            if not self._can_auto_close(ctx):
                logger.info(
                    "PR #%s is protected from auto-close; continuing validation",
                    ctx.number,
                )
            else:
                logger.info("PR #%s considered spam, closing", ctx.number)
                SPAM_COMMENT = (
                    f"{SPAM_CLOSE_COMMENT} Diagnostics: {' '.join(spam_comments)}"
                )
                ok = self._comment(ctx, SPAM_COMMENT)
                return self.client.close_pr(ctx.number) and ok

        ok = True
        if not ctx.is_draft:
            logger.info(
                "PR #%s is not a draft, removing work-in-progress label", ctx.number
            )
            ok = self.client.remove_labels(ctx.number, LABEL_WORK_IN_PROGRESS) and ok

        validation = validate_pr_structure(ctx, checklist, appid)
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

        review = self.client.fetch_review_state(pr_num)
        if review is None:
            logger.info("Failed to fetch review state on PR #%s", ctx.number)
            return False

        return self.update_review_state(ctx, review) and ok

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
