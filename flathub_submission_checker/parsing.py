import logging

from flathub_submission_checker.constants import (
    ADD_PREFIX_RE,
    APPID_COMPONENT_RE,
    CHECKLIST_ITEMS,
    CHECKLIST_LINE_RE,
    MAX_UNCHECKED_ITEMS_ALLOWED,
    REQUIRED_CHECKLIST_COUNT,
    ROLE_CHECKLIST_RE,
    VIDEO_CHECKLIST_ITEM,
    VIDEO_LINK_RE,
    VIDEO_LOOKAHEAD_LINES,
    VIDEO_NA_RE,
)

logger = logging.getLogger(__name__)


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


def _role_checklist_matches(text: str) -> bool:
    return bool(ROLE_CHECKLIST_RE.search(text)) and "the project" in text.lower()


def _checklist_item_matches(text: str) -> bool:
    return any(item in text for item in CHECKLIST_ITEMS) or _role_checklist_matches(
        text
    )


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
