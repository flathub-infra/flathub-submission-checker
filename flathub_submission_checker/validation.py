import logging

from publicsuffixlist import PublicSuffixList  # type: ignore[import-untyped]

from flathub_submission_checker.constants import (
    ADDON_COMPONENTS,
    BASE_REVIEW_COMMENT,
    CODE_HOST_PREFIXES,
    DOMAIN_COMMENT_PARTIAL,
    EXCLUDED_ID_PREFIXES,
    FLATHUB_JSON_RE,
    MASTER_BRANCH_URL,
    MAX_UNCHECKED_ITEMS_ALLOWED,
    PR_TEMPLATE_URL,
    REQUIREMENTS_URL,
    RUNTIME_PREFIXES,
    SUBMISSION_URL,
    TOPLEVEL_MANIFEST_RE,
    VERIFICATION_URL,
)
from flathub_submission_checker.models import PRContext, ValidationResult
from flathub_submission_checker.parsing import (
    checklist_fully_checked,
    checklist_matches_template,
    count_unchecked_relevant_items,
    get_appid_from_pr_title,
    has_missing_video,
    parse_checklist,
)

logger = logging.getLogger(__name__)


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


def is_considered_spam(files: list[str], body: str) -> tuple[bool, str]:
    if files and all("/" in f for f in files):
        logger.info(
            "All files are nested in subdirectories, flagging as spam: %s", files
        )
        return (True, "Files not in toplevel")

    checklist = parse_checklist(body)

    if not checklist_matches_template(checklist):
        logger.info("Checklist missing or altered, flagging as spam")
        return (True, "Checklist(s) not completed or missing")

    if has_missing_video(body):
        logger.info(
            "Video checklist item missing, unchecked, or has no link, flagging as spam"
        )
        return (True, "Video checklist requirement not met")

    unchecked_count = count_unchecked_relevant_items(checklist)
    result = unchecked_count > MAX_UNCHECKED_ITEMS_ALLOWED
    logger.info(
        "Unchecked checklist count is %s > %s",
        unchecked_count,
        MAX_UNCHECKED_ITEMS_ALLOWED,
    )
    if result:
        return (result, "Checklist(s) not completed or missing")

    return (False, "")


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
