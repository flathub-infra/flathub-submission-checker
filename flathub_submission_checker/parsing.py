import logging
import re
from datetime import datetime

from flathub_submission_checker.constants import (
    ADD_PREFIX_RE,
    APPID_COMPONENT_RE,
    CHECKLIST_ITEMS,
    CHECKLIST_LINE_RE,
    FLATHUB_DOCS_BASE_URL,
    MARKDOWN_LINK_DEFINITION_RE,
    MARKDOWN_LINK_LABEL_RE,
    ROLE_CHECKLIST_RE,
    VIDEO_CHECKLIST_ITEM,
    VIDEO_LINK_RE,
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
    checklist: list[tuple[bool, str]] = []
    pending: tuple[bool, str, int] | None = None
    continuation: list[str] = []

    def flush() -> None:
        nonlocal pending, continuation
        if pending is None:
            return
        checked, text, _ = pending
        if continuation:
            text = " ".join([text, *continuation])
        checklist.append((checked, text))
        pending = None
        continuation = []

    for line in body.splitlines():
        matched = CHECKLIST_LINE_RE.match(line)
        if matched:
            flush()
            pending = (
                matched.group(1).lower() == "x",
                matched.group(2).strip(),
                _indentation_width(line),
            )
            continue
        if (
            pending is not None
            and line.strip()
            and not re.match(
                r"^[ \t]*(?:#{1,6}(?:\s|$)|>|[-+*](?:\s|$)|"
                r"\d{1,9}[.)]\s|`{3,}|~{3,}|<)",
                line,
            )
        ):
            continuation.append(line.strip())
            continue
        flush()

    flush()
    logger.info("Found %s checklist line(s)", len(checklist))

    unchecked = [text for checked, text in checklist if not checked]
    if unchecked:
        logger.info("Found unchecked line(s): %s", unchecked)

    return checklist


def _normalize_checklist_text(text: str) -> str:
    normalized = text.replace("*", "").replace("_", "").replace("`", "")
    return " ".join(normalized.split()).casefold()


def _role_checklist_matches(text: str) -> bool:
    return bool(ROLE_CHECKLIST_RE.search(_normalize_checklist_text(text)))


def _checklist_items_for_date(created_at: datetime | None) -> tuple[str, ...]:
    return tuple(
        item
        for item, effective_date in CHECKLIST_ITEMS
        if effective_date is None
        or (created_at is not None and effective_date <= created_at)
    )


def _checklist_item_matches(text: str, items: tuple[str, ...]) -> bool:
    normalized_text = _normalize_checklist_text(text)
    return any(
        _normalize_checklist_text(item) in normalized_text for item in items
    ) or _role_checklist_matches(normalized_text)


ROLE_CHECKLIST_ITEM_NAME = (
    "Your role in the project (author, developer, or upstream contributor)"
)


def missing_checklist_items(
    checklist: list[tuple[bool, str]], created_at: datetime | None
) -> list[str]:
    items = _checklist_items_for_date(created_at)
    texts = [_normalize_checklist_text(text) for _, text in checklist]

    missing_items = [
        item
        for item in items
        if not any(_normalize_checklist_text(item) in text for text in texts)
    ]

    if not any(_role_checklist_matches(text) for text in texts):
        missing_items.append(ROLE_CHECKLIST_ITEM_NAME)

    if missing_items:
        logger.info("Found missing required item(s): %s", missing_items)
    return missing_items


def _matching_checklist_item(text: str, items: tuple[str, ...]) -> str | None:
    normalized_text = _normalize_checklist_text(text)
    return next(
        (item for item in items if _normalize_checklist_text(item) in normalized_text),
        None,
    )


def unchecked_checklist_items(
    checklist: list[tuple[bool, str]], created_at: datetime | None
) -> list[str]:
    items = _checklist_items_for_date(created_at)
    not_yet_effective_items = tuple(
        item
        for item, effective_date in CHECKLIST_ITEMS
        if effective_date is not None
        and (created_at is None or effective_date > created_at)
    )

    unchecked_items: list[str] = []
    for checked, text in checklist:
        if checked:
            continue
        matched_item = _matching_checklist_item(text, items)
        if matched_item is not None:
            unchecked_items.append(matched_item)
            continue
        if _role_checklist_matches(_normalize_checklist_text(text)):
            unchecked_items.append(ROLE_CHECKLIST_ITEM_NAME)
            continue
        if _matching_checklist_item(text, not_yet_effective_items) is not None:
            continue
        unchecked_items.append(text)

    if unchecked_items:
        logger.info("Found unchecked relevant item(s): %s", unchecked_items)
    return unchecked_items


def checklist_matches_template(
    checklist: list[tuple[bool, str]], created_at: datetime | None
) -> bool:
    return not missing_checklist_items(checklist, created_at)


def checklist_fully_checked(
    checklist: list[tuple[bool, str]], created_at: datetime | None
) -> bool:
    if not checklist_matches_template(checklist, created_at):
        return False
    return not unchecked_checklist_items(checklist, created_at)


def count_unchecked_relevant_items(
    checklist: list[tuple[bool, str]], created_at: datetime | None
) -> int:
    items = _checklist_items_for_date(created_at)
    relevant = [
        checked for checked, text in checklist if _checklist_item_matches(text, items)
    ]
    unchecked_count = sum(1 for checked in relevant if not checked)
    logger.info(
        "Found %s relevant checklists and %s relevant but unchecked checklists",
        len(relevant),
        unchecked_count,
    )
    return unchecked_count


def _indentation_width(line: str) -> int:
    expanded_line = line.expandtabs(4)
    return len(expanded_line) - len(expanded_line.lstrip())


def _checklist_item_texts(
    lines: list[str], index: int, text: str, indent: int
) -> tuple[str, str]:
    item_lines = [text]
    search_lines = [text]
    for continuation in lines[index + 1 :]:
        if (
            CHECKLIST_LINE_RE.match(continuation)
            and _indentation_width(continuation) <= indent
        ):
            break
        search_lines.append(continuation)
        if not CHECKLIST_LINE_RE.match(continuation):
            item_lines.append(continuation)
    return "\n".join(item_lines), "\n".join(search_lines)


def has_missing_video(body: str) -> bool:
    lines = body.splitlines()
    link_definitions = {
        _normalize_checklist_text(label): url
        for label, url in MARKDOWN_LINK_DEFINITION_RE.findall(body)
    }
    normalized_video_item = _normalize_checklist_text(VIDEO_CHECKLIST_ITEM)

    for i, line in enumerate(lines):
        matched = CHECKLIST_LINE_RE.match(line)
        if not matched:
            continue
        video_indent = _indentation_width(line)
        item_text, search_text = _checklist_item_texts(
            lines, i, matched.group(2), video_indent
        )
        if normalized_video_item not in _normalize_checklist_text(item_text):
            continue
        if matched.group(1).lower() != "x":
            logger.info("Video checklist item is unchecked")
            return True

        referenced_urls = [
            link_definitions[key]
            for label in MARKDOWN_LINK_LABEL_RE.findall(search_text)
            if (key := _normalize_checklist_text(label)) in link_definitions
        ]
        search_text = "\n".join([search_text, *referenced_urls])

        for video_link in VIDEO_LINK_RE.finditer(search_text):
            if not video_link.group(0).startswith(f"{FLATHUB_DOCS_BASE_URL}/"):
                return False

        if VIDEO_NA_RE.search(search_text):
            logger.info("Video checklist item marked N/A or no video available")
            return True
        logger.info("Video checklist item has no acceptable link")
        return True

    logger.info("Video checklist item not found in PR body")
    return True
