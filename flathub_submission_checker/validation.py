import logging
from datetime import datetime

from publicsuffixlist import PublicSuffixList  # type: ignore[import-untyped]

from flathub_submission_checker.constants import (
    ADDON_COMPONENTS,
    BASE_REVIEW_COMMENT,
    CODE_HOST_PREFIXES,
    DOMAIN_COMMENT_PARTIAL,
    EXCLUDED_ID_PREFIXES,
    FLATHUB_JSON_RE,
    LABEL_MIGRATE,
    MASTER_BRANCH_URL,
    MAX_UNCHECKED_ITEMS_ALLOWED,
    PR_TEMPLATE_URL,
    REQUIREMENTS_URL,
    RUNTIME_PREFIXES,
    SUBMISSION_URL,
    TOPLEVEL_MANIFEST_RE,
    VERIFICATION_URL,
    VIDEO_CHECKLIST_ITEM,
)
from flathub_submission_checker.models import PRContext, ValidationResult
from flathub_submission_checker.parsing import (
    count_unchecked_relevant_items,
    has_missing_video,
    missing_checklist_items,
    unchecked_checklist_items,
)

logger = logging.getLogger(__name__)


def demangle(name: str) -> str:
    return name.removeprefix("_").replace("_", "-")


def is_appid_addon(appid: str | None) -> bool:
    if not appid or appid.count(".") < 2:
        return False
    return appid.split(".")[-2].lower() in ADDON_COMPONENTS


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

    if is_appid_addon(appid):
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


def _quote_items(items: list[str]) -> str:
    return ", ".join(f'"{item}"' for item in items)


def _checklist_diagnostics(
    checklist: list[tuple[bool, str]], created_at: datetime | None
) -> list[str]:
    missing_items = missing_checklist_items(checklist, created_at)
    unchecked_items = unchecked_checklist_items(checklist, created_at)

    reasons: list[str] = []
    if missing_items:
        reasons.append(
            f"- Add the missing [checklists]({PR_TEMPLATE_URL}) item(s): "
            f"{_quote_items(missing_items)}"
        )
    if unchecked_items:
        reasons.append(
            f"- Complete and check the [checklists]({PR_TEMPLATE_URL}) item(s): "
            f"{_quote_items(unchecked_items)}"
        )
    return reasons


def is_considered_spam(
    checklist: list[tuple[bool, str]],
    files: list[str],
    body: str,
    labels: set[str],
    appid: str | None,
    created_at: datetime | None,
) -> tuple[bool, list[str]]:
    diagnostics: list[str] = []

    if files and all("/" in f for f in files):
        logger.info(
            "All files are nested in subdirectories, flagging as spam: %s", files
        )
        diagnostics.append(
            "Move the Flatpak manifest from its subdirectory to the top level "
            "of the repository."
        )

    missing_items = missing_checklist_items(checklist, created_at)
    if missing_items:
        logger.info("Checklist missing or altered, flagging as spam")
        diagnostics.append(
            f"Add the missing checklist item(s): {_quote_items(missing_items)}."
        )

    video_missing = (
        LABEL_MIGRATE not in labels
        and has_missing_video(body)
        and not is_appid_addon(appid)
    )
    if video_missing and VIDEO_CHECKLIST_ITEM not in missing_items:
        logger.info(
            "Video checklist item missing, unchecked, or has no link, flagging as spam"
        )
        diagnostics.append(
            "Check the video checklist item and attach a video showing the "
            "application running as a Flatpak on Linux."
        )

    unchecked_count = count_unchecked_relevant_items(checklist, created_at)
    logger.info(
        "Unchecked checklist count is %s > %s",
        unchecked_count,
        MAX_UNCHECKED_ITEMS_ALLOWED,
    )
    if unchecked_count > MAX_UNCHECKED_ITEMS_ALLOWED:
        diagnostics.append(
            "Complete and check the unchecked checklist item(s): "
            f"{_quote_items(unchecked_checklist_items(checklist, created_at))}."
        )

    return (bool(diagnostics), diagnostics)


def validate_pr_structure(
    ctx: PRContext, checklist: list[tuple[bool, str]], appid: str | None
) -> ValidationResult:
    checks: list[tuple[bool, str]] = [
        (appid is not None, '- PR title is "Add $FLATPAK_ID"'),
        (
            not ctx.has_master_commit,
            "- PR does not contain commits from the "
            f"[master branch]({MASTER_BRANCH_URL})",
        ),
        (
            not any(FLATHUB_JSON_RE.match(f) for f in ctx.files),
            "- Move the flathub.json file to the top level of the repository",
        ),
        (
            any(TOPLEVEL_MANIFEST_RE.match(f) for f in ctx.files),
            "- Add or move the Flatpak manifest to the top level of the repository",
        ),
    ]

    reasons = [message for passed, message in checks if not passed]
    reasons.extend(_checklist_diagnostics(checklist, ctx.created_at))
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
