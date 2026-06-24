from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from flathub_submission_checker.constants import (
    BUILD_START_COMMENT_PARTIAL,
    BUILD_SUCCESS_COMMENT,
    DOMAIN_COMMENT_PARTIAL,
    LABEL_AWAITING_CHANGES,
    LABEL_AWAITING_REVIEW,
    LABEL_BLOCKED,
    LABEL_LEAVE_OPEN,
    LABEL_PR_CHECK_BLOCKED,
    LABEL_REVIEWED_WAITING,
    LABEL_WORK_IN_PROGRESS,
    MAX_UNCHECKED_ITEMS_ALLOWED,
    REQUIRED_CHECKLIST_COUNT,
    REVIEW_COMMENT_PARTIAL,
    SPAM_CLOSE_COMMENT,
)
from flathub_submission_checker.models import PRContext, has_master_commit
from flathub_submission_checker.parsing import (
    checklist_fully_checked,
    checklist_matches_template,
    count_unchecked_relevant_items,
    get_appid_from_pr_title,
    has_missing_video,
    parse_checklist,
)
from flathub_submission_checker.validation import (
    build_domain_comment,
    build_review_comment,
    get_domain,
    is_considered_spam,
    validate_pr_structure,
)
from flathub_submission_checker.validator import (
    PRValidator,
    should_demote_to_awaiting_changes,
    should_mark_awaiting_review,
    should_post_domain_comment,
    should_promote_to_awaiting_review,
    should_start_build,
)

ROLE_LINE = "I am the author/developer/upstream contributor to the project."

NON_ROLE_ITEMS = """\
- [x] Please describe the application briefly.
- [x] Please attach a video showcasing the application on Linux using the Flatpak.
      https://example.com/demo-video.mp4
- [x] The Flatpak ID follows all the rules listed in the requirements.
- [x] I have read and followed all the submission guidelines.
"""


def checklist_body(role_line: str = ROLE_LINE, unchecked: int = 0) -> str:
    lines: list[str] = [*NON_ROLE_ITEMS.strip().split("\n")]
    checklist_line_indices = [
        i for i, line in enumerate(lines) if line.startswith("- [x]")
    ]
    for idx in checklist_line_indices[:unchecked]:
        lines[idx] = lines[idx].replace("[x]", "[ ]", 1)
    lines.append(f"- [x] {role_line}")
    return "\n".join(lines) + "\n"


FULL_CHECKLIST_BODY = checklist_body()
PARTIAL_CHECKLIST_BODY = checklist_body(unchecked=1)
MISSING_ITEM_CHECKLIST_BODY = NON_ROLE_ITEMS
NO_CHECKLIST_BODY = "Just a plain PR description with no checklist at all."

VALID_FILES = [
    ".github/workflows/update_sources.yaml",
    "com.example.foobar.json",
    "cargo-sources.json",
]


@dataclass
class FakeUser:
    login: str


@dataclass
class FakeComment:
    body: str
    user: FakeUser


@dataclass
class FakeCommitInner:
    email: str
    message: str

    @property
    def author(self):
        return self

    @property
    def commit(self):
        return self


@dataclass
class FakeRawCommit:
    _commit: FakeCommitInner

    @property
    def commit(self):
        return self._commit


@dataclass
class FakeLabel:
    name: str


@dataclass
class FakeFile:
    filename: str


@dataclass
class FakeRawPR:
    number: int
    title: str | None
    body: str | None
    draft: bool
    _comments: list[FakeComment] = field(default_factory=list)
    _files: list[FakeFile] = field(default_factory=list)
    _labels: list[FakeLabel] = field(default_factory=list)
    _commits: list[FakeRawCommit] = field(default_factory=list)

    def get_issue_comments(self):
        return self._comments

    def get_files(self):
        return self._files

    def get_labels(self):
        return self._labels

    def get_commits(self):
        return self._commits


def make_pr_context(
    number: int = 7378,
    title: str = "Add com.example.foobar",
    body: str = FULL_CHECKLIST_BODY,
    is_draft: bool = False,
    files: list[str] | None = None,
    labels: set[str] | None = None,
    comment_lines: list[str] | None = None,
    has_master_commit_: bool = False,
) -> PRContext:
    return PRContext(
        number=number,
        title=title,
        body=body,
        is_draft=is_draft,
        files=files if files is not None else list(VALID_FILES),
        labels=labels if labels is not None else set(),
        comment_lines=comment_lines if comment_lines is not None else [],
        has_master_commit=has_master_commit_,
    )


def make_fake_raw_pr(
    number: int = 7378,
    title: str = "Add com.example.foobar",
    body: str = FULL_CHECKLIST_BODY,
    draft: bool = False,
    comments: list[FakeComment] | None = None,
    files: list[str] | None = None,
    labels: list[str] | None = None,
    commits: list[FakeRawCommit] | None = None,
) -> FakeRawPR:
    return FakeRawPR(
        number=number,
        title=title,
        body=body,
        draft=draft,
        _comments=[FakeComment(body=c.body, user=c.user) for c in (comments or [])],
        _files=[FakeFile(filename=f) for f in (files or VALID_FILES)],
        _labels=[FakeLabel(name=n) for n in (labels or [])],
        _commits=commits or [],
    )


def make_client(
    fetch_pr=None,
    unresolved_threads: int = 0,
    add_labels_ok: bool = True,
    remove_labels_ok: bool = True,
    post_comment_ok: bool = True,
    close_pr_ok: bool = True,
    pr_numbers: list[int] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.fetch_pull_request.return_value = fetch_pr
    client.count_unresolved_review_threads.return_value = unresolved_threads
    client.add_labels.return_value = add_labels_ok
    client.remove_labels.return_value = remove_labels_ok
    client.post_comment.return_value = post_comment_ok
    client.close_pr.return_value = close_pr_ok
    client.fetch_pr_numbers.return_value = pr_numbers if pr_numbers is not None else []
    return client


class TestAppIdFromTitle:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Add com.example.App", "com.example.App"),
            ("Add org.example.some.App", "org.example.some.App"),
            ("ADD com.example.App", "com.example.App"),
            ("add com.example.App", "com.example.App"),
            ("Update com.example.App", None),
            ("Add com.example", None),
            ("Add 1com.example.App", None),
            ("Add com.my-app.Foo", "com.my-app.Foo"),
            ("Add com.example.My_App", "com.example.My_App"),
        ],
    )
    def test_extracts_or_rejects(self, title, expected):
        assert get_appid_from_pr_title(title) == expected


class TestGetDomain:
    @pytest.mark.parametrize(
        ("appid", "expected"),
        [
            ("com.example.foobar", "example.com"),
            ("com.github.someuser.App", None),
            ("com.gitlab.someuser.App", None),
            ("io.github.someuser.App", None),
            ("com.example.App.BaseApp", None),
            ("com.example.addon.Something", None),
            ("com.example.extension.Something", None),
            ("com.example.plugin.Something", None),
            ("org.freedesktop.Platform.GL.default", None),
            ("org.kde.Platform.SomeThing", None),
            ("page.codeberg.someuser.App", "someuser.codeberg.page"),
            ("io.sourceforge.myproject.App", "myproject.sourceforge.io"),
            ("org.kde.Kate", None),
            ("org.gnome.TextEditor", None),
        ],
    )
    def test_derives_or_excludes(self, appid, expected):
        assert get_domain(appid) == expected

    def test_underscored_name_demangled(self):
        domain = get_domain("com._example.App")
        assert domain is not None
        assert "example" in domain


class TestParseChecklist:
    def test_parses_checked_and_unchecked_items(self):
        body = "- [x] Do something\n- [X] Do another\n- [ ] Skip this\n"
        assert parse_checklist(body) == [
            (True, "Do something"),
            (True, "Do another"),
            (False, "Skip this"),
        ]

    def test_empty_body_returns_empty_list(self):
        assert parse_checklist(NO_CHECKLIST_BODY) == []

    def test_full_checklist_body(self):
        result = parse_checklist(FULL_CHECKLIST_BODY)
        assert len(result) == 5
        assert all(checked for checked, _ in result)

    def test_partial_checklist_body(self):
        result = parse_checklist(PARTIAL_CHECKLIST_BODY)
        assert [checked for checked, _ in result] == [False, True, True, True, True]


class TestChecklistMatchesTemplate:
    def test_full_checklist_matches(self):
        assert checklist_matches_template(parse_checklist(FULL_CHECKLIST_BODY)) is True

    def test_partial_checklist_still_matches_template(self):
        assert (
            checklist_matches_template(parse_checklist(PARTIAL_CHECKLIST_BODY)) is True
        )

    def test_unrelated_body_does_not_match(self):
        checklist = parse_checklist("- [x] Some random item\n- [x] Another item\n")
        assert checklist_matches_template(checklist) is False

    @pytest.mark.parametrize(
        ("role_line", "expected"),
        [
            ("I am the author/developer/upstream contributor to the project.", True),
            ("I am an author to the project.", True),
            ("I am a contributor to the project, occasionally.", True),
            ("I am the developer of the project", True),
            ("I am an upstream contributor to the project.", True),
            ("I am the maintainer to the project.", True),
            ("I am an author to the codebase.", True),
        ],
    )
    def test_role_line_variants(self, role_line, expected):
        checklist = parse_checklist(checklist_body(role_line=role_line))
        assert checklist_matches_template(checklist) is expected


class TestChecklistFullyChecked:
    def test_all_checked_returns_true(self):
        assert checklist_fully_checked(parse_checklist(FULL_CHECKLIST_BODY)) is True

    def test_one_unchecked_returns_false(self):
        assert checklist_fully_checked(parse_checklist(PARTIAL_CHECKLIST_BODY)) is False

    def test_missing_role_item_returns_false(self):
        checklist = parse_checklist(MISSING_ITEM_CHECKLIST_BODY)
        assert checklist_fully_checked(checklist) is False

    def test_empty_checklist_returns_false(self):
        assert checklist_fully_checked(parse_checklist(NO_CHECKLIST_BODY)) is False


class TestCountUncheckedRelevantItems:
    def test_fully_checked_has_zero_unchecked(self):
        checklist = parse_checklist(FULL_CHECKLIST_BODY)
        assert count_unchecked_relevant_items(checklist) == 0

    @pytest.mark.parametrize("unchecked", [1, 2, 3])
    def test_unchecked_count_matches_input(self, unchecked):
        checklist = parse_checklist(checklist_body(unchecked=unchecked))
        assert count_unchecked_relevant_items(checklist) == unchecked

    def test_role_line_unchecked_also_counted(self):
        checklist = parse_checklist(checklist_body(unchecked=4)).copy()
        _, text = checklist[-1]
        checklist[-1] = (False, text)
        assert count_unchecked_relevant_items(checklist) == REQUIRED_CHECKLIST_COUNT

    def test_unrelated_items_are_not_counted(self):
        checklist = parse_checklist("- [x] Buy Flathub\n- [ ] Sell Flathub\n")
        assert count_unchecked_relevant_items(checklist) == 0


class TestIsConsideredSpam:
    def test_valid_submission_is_not_spam(self):
        assert is_considered_spam(
            parse_checklist(FULL_CHECKLIST_BODY),
            VALID_FILES,
            FULL_CHECKLIST_BODY,
            set(),
        ) == (
            False,
            "",
        )

    def test_all_files_in_subdirectory_is_spam(self):
        nested_files = ["some/nested/file.json", "another/nested/file.yaml"]
        assert is_considered_spam(
            parse_checklist(FULL_CHECKLIST_BODY),
            nested_files,
            FULL_CHECKLIST_BODY,
            set(),
        ) == (
            True,
            "Files not in toplevel",
        )

    def test_missing_checklist_template_is_spam(self):
        assert is_considered_spam(
            parse_checklist(NO_CHECKLIST_BODY), VALID_FILES, NO_CHECKLIST_BODY, set()
        ) == (
            True,
            "Checklist(s) not completed or missing",
        )

    def test_incomplete_checklist_template_is_not_spam(self):
        assert is_considered_spam(
            parse_checklist(MISSING_ITEM_CHECKLIST_BODY),
            VALID_FILES,
            MISSING_ITEM_CHECKLIST_BODY,
            set(),
        ) == (
            False,
            "",
        )

    @pytest.mark.parametrize("unchecked", [2, 3, 4])
    def test_too_many_unchecked_items_is_spam(self, unchecked):
        lines = NON_ROLE_ITEMS.strip().split("\n")
        non_video_indices = [
            i
            for i, line in enumerate(lines)
            if line.startswith("- [x]") and "video" not in line
        ]
        for idx in non_video_indices[:unchecked]:
            lines[idx] = lines[idx].replace("[x]", "[ ]", 1)
        lines.append(f"- [x] {ROLE_LINE}")
        body = "\n".join(lines) + "\n"

        assert is_considered_spam(parse_checklist(body), VALID_FILES, body, set()) == (
            True,
            "Checklist(s) not completed or missing",
        )

    def test_missing_video_item_is_spam(self):
        body = checklist_body().replace(
            "      https://example.com/demo-video.mp4\n", ""
        )
        assert is_considered_spam(parse_checklist(body), VALID_FILES, body, set()) == (
            True,
            "Video checklist requirement not met",
        )

    def test_missing_video_item_but_migrate_appid(self):
        body = checklist_body().replace(
            "      https://example.com/demo-video.mp4\n", ""
        )
        assert is_considered_spam(
            parse_checklist(body), VALID_FILES, body, {"migrate-app-id"}
        ) == (
            False,
            "",
        )

    def test_one_unchecked_item_is_not_spam(self):
        body = checklist_body(unchecked=1)
        assert is_considered_spam(parse_checklist(body), VALID_FILES, body, set()) == (
            False,
            "",
        )

    def test_unchecked_boundary_matches_max_allowed_constant(self):
        assert MAX_UNCHECKED_ITEMS_ALLOWED == 1


class TestHasMasterCommit:
    def test_detected_as_second_commit(self):
        inner1 = FakeCommitInner(email="someone@example.com", message="Initial commit")
        inner2 = FakeCommitInner(
            email="mclasen@redhat.com", message="Add some instructions"
        )
        pr = make_fake_raw_pr(commits=[FakeRawCommit(inner1), FakeRawCommit(inner2)])
        assert has_master_commit(pr) is True

    def test_not_detected_when_second_commit_differs(self):
        inner1 = FakeCommitInner(email="someone@example.com", message="Initial commit")
        inner2 = FakeCommitInner(email="someone@example.com", message="Another commit")
        pr = make_fake_raw_pr(commits=[FakeRawCommit(inner1), FakeRawCommit(inner2)])
        assert has_master_commit(pr) is False

    def test_not_detected_with_single_commit(self):
        inner = FakeCommitInner(
            email="mclasen@redhat.com", message="Add some instructions"
        )
        pr = make_fake_raw_pr(commits=[FakeRawCommit(inner)])
        assert has_master_commit(pr) is False


class TestPRContextFromPullRequest:
    def test_builds_context_from_fake_pr(self):
        bot_comment = FakeComment(
            body="Starting a test build of the submission",
            user=FakeUser(login="github-actions"),
        )
        pr = make_fake_raw_pr(
            number=7378,
            title="Add com.example.foobar",
            body=FULL_CHECKLIST_BODY,
            comments=[bot_comment],
            files=VALID_FILES,
            labels=["ready"],
        )
        ctx = PRContext.from_pull_request(pr)
        assert ctx.number == 7378
        assert ctx.title == "Add com.example.foobar"
        assert ctx.is_draft is False
        assert "com.example.foobar.json" in ctx.files
        assert "ready" in ctx.labels
        assert any("Starting a test build" in line for line in ctx.comment_lines)

    def test_excludes_non_bot_comments(self):
        user_comment = FakeComment(
            body="Please review my PR", user=FakeUser(login="some-human")
        )
        pr = make_fake_raw_pr(comments=[user_comment])
        ctx = PRContext.from_pull_request(pr)
        assert not any("Please review my PR" in line for line in ctx.comment_lines)

    def test_strips_carriage_returns_from_body(self):
        pr = make_fake_raw_pr(body="line1\r\nline2\r\n")
        ctx = PRContext.from_pull_request(pr)
        assert "\r" not in ctx.body

    def test_draft_flag_carried_over(self):
        pr = make_fake_raw_pr(draft=True)
        assert PRContext.from_pull_request(pr).is_draft is True


class TestPRContextMethods:
    def test_comment_exists_finds_substring(self):
        ctx = make_pr_context(comment_lines=["Starting a test build of the submission"])
        assert ctx.comment_exists("Starting a test build") is True
        assert ctx.comment_exists("build failed") is False

    def test_comment_exists_any_finds_first_match(self):
        ctx = make_pr_context(comment_lines=["build succeeded"])
        assert ctx.comment_exists_any("nothing", "build succeeded") is True
        assert ctx.comment_exists_any("nothing", "also nothing") is False

    def test_has_any_label(self):
        ctx = make_pr_context(labels={"awaiting-review", "ready"})
        assert ctx.has_any_label("awaiting-review") is True
        assert ctx.has_any_label("blocked") is False
        assert ctx.has_any_label("blocked", "awaiting-review") is True

    def test_latest_build_succeeded_when_last_line_is_success(self):
        ctx = make_pr_context(
            comment_lines=["Test build failed", "Test build [Test build succeeded]"]
        )
        assert ctx.latest_build_succeeded(BUILD_SUCCESS_COMMENT) is True

    def test_latest_build_not_succeeded_when_last_line_is_failure(self):
        ctx = make_pr_context(
            comment_lines=["Test build [Test build succeeded]", "Test build failed"]
        )
        assert ctx.latest_build_succeeded(BUILD_SUCCESS_COMMENT) is False

    def test_latest_build_not_succeeded_with_no_build_lines(self):
        ctx = make_pr_context(comment_lines=["Some unrelated comment"])
        assert ctx.latest_build_succeeded(BUILD_SUCCESS_COMMENT) is False

    def test_record_comment_appends_lines(self):
        ctx = make_pr_context(comment_lines=[])
        ctx.record_comment("line one\nline two")
        assert "line one" in ctx.comment_lines
        assert "line two" in ctx.comment_lines


class TestValidatePRStructure:
    def test_valid_pr_passes_all_checks(self):
        ctx = make_pr_context(
            title="Add com.example.foobar",
            body=FULL_CHECKLIST_BODY,
            files=["com.example.foobar.json", "cargo-sources.json"],
        )
        result = validate_pr_structure(ctx, parse_checklist(FULL_CHECKLIST_BODY))
        assert result.is_valid is True
        assert result.reasons == []
        assert result.domain == "example.com"

    def test_wrong_title_format_fails(self):
        result = validate_pr_structure(
            make_pr_context(title="Update com.example.foobar"), []
        )
        assert result.is_valid is False
        assert any("PR title" in r for r in result.reasons)

    def test_master_commit_present_fails(self):
        result = validate_pr_structure(make_pr_context(has_master_commit_=True), [])
        assert result.is_valid is False
        assert any("master branch" in r for r in result.reasons)

    def test_flathub_json_in_subdirectory_fails(self):
        ctx = make_pr_context(files=["com.example.foobar.json", "subdir/flathub.json"])
        result = validate_pr_structure(ctx, [])
        assert result.is_valid is False
        assert any("flathub.json" in r for r in result.reasons)

    def test_no_toplevel_manifest_fails(self):
        ctx = make_pr_context(
            files=["subdir/com.example.foobar.json", "subdir/cargo-sources.json"]
        )
        result = validate_pr_structure(ctx, [])
        assert result.is_valid is False
        assert any("manifest" in r for r in result.reasons)

    def test_incomplete_checklist_fails(self):
        body = checklist_body(unchecked=2)
        ctx = make_pr_context(body=body)
        result = validate_pr_structure(ctx, parse_checklist(body))
        assert result.is_valid is False
        assert any("checklists" in r for r in result.reasons)

    def test_domain_none_for_excluded_prefix(self):
        ctx = make_pr_context(
            title="Add io.github.user.App",
            body=FULL_CHECKLIST_BODY,
            files=["io.github.user.App.json"],
        )
        assert (
            validate_pr_structure(ctx, parse_checklist(FULL_CHECKLIST_BODY)).domain
            is None
        )

    def test_multiple_failures_all_reported(self):
        ctx = make_pr_context(
            title="Update com.example.foobar",
            body=MISSING_ITEM_CHECKLIST_BODY,
            has_master_commit_=True,
        )
        result = validate_pr_structure(
            ctx, parse_checklist(MISSING_ITEM_CHECKLIST_BODY)
        )
        assert result.is_valid is False
        assert len(result.reasons) >= 2


class TestShouldStartBuild:
    def test_starts_on_clean_pr(self):
        assert (
            should_start_build(make_pr_context(labels=set(), comment_lines=[])) is True
        )

    @pytest.mark.parametrize("label", [LABEL_BLOCKED, LABEL_PR_CHECK_BLOCKED])
    def test_skipped_when_blocking_label_present(self, label):
        assert should_start_build(make_pr_context(labels={label})) is False

    def test_skipped_if_already_started(self):
        ctx = make_pr_context(comment_lines=[BUILD_START_COMMENT_PARTIAL])
        assert should_start_build(ctx) is False

    def test_skipped_if_already_succeeded(self):
        ctx = make_pr_context(comment_lines=[f"Test build {BUILD_SUCCESS_COMMENT}"])
        assert should_start_build(ctx) is False


class TestShouldPostDomainComment:
    def test_posts_on_fresh_pr(self):
        ctx = make_pr_context(labels=set(), comment_lines=[])
        assert should_post_domain_comment(ctx, "example.com") is True

    def test_skips_if_no_domain(self):
        assert should_post_domain_comment(make_pr_context(), None) is False

    def test_skips_if_blocked(self):
        ctx = make_pr_context(labels={LABEL_BLOCKED})
        assert should_post_domain_comment(ctx, "example.com") is False

    def test_skips_if_already_commented(self):
        verif_url = "https://example.com/.well-known/org.flathub.VerifiedApps.txt"
        ctx = make_pr_context(comment_lines=[f"upload a file to {verif_url}"])
        assert should_post_domain_comment(ctx, "example.com") is False


class TestShouldMarkAwaitingReview:
    def test_marks_clean_pr(self):
        assert should_mark_awaiting_review(make_pr_context(labels=set())) is True

    @pytest.mark.parametrize(
        "label",
        [LABEL_AWAITING_CHANGES, LABEL_BLOCKED, LABEL_REVIEWED_WAITING],
    )
    def test_skipped_with_conflicting_label(self, label):
        assert should_mark_awaiting_review(make_pr_context(labels={label})) is False


class TestShouldDemoteAndPromote:
    def test_demotes_when_threads_open(self):
        ctx = make_pr_context(labels={LABEL_AWAITING_REVIEW})
        assert should_demote_to_awaiting_changes(ctx, unresolved_threads=2) is True

    def test_no_demote_without_unresolved_threads(self):
        ctx = make_pr_context(labels={LABEL_AWAITING_REVIEW})
        assert should_demote_to_awaiting_changes(ctx, unresolved_threads=0) is False

    def test_no_demote_when_not_awaiting_review(self):
        ctx = make_pr_context(labels={LABEL_AWAITING_CHANGES})
        assert should_demote_to_awaiting_changes(ctx, unresolved_threads=3) is False

    def test_promotes_when_build_succeeded_and_no_threads(self):
        ctx = make_pr_context(
            labels={LABEL_AWAITING_CHANGES},
            comment_lines=[f"Test build {BUILD_SUCCESS_COMMENT}"],
        )
        assert should_promote_to_awaiting_review(ctx, unresolved_threads=0) is True

    def test_no_promote_with_unresolved_threads(self):
        ctx = make_pr_context(
            labels={LABEL_AWAITING_CHANGES},
            comment_lines=[f"Test build {BUILD_SUCCESS_COMMENT}"],
        )
        assert should_promote_to_awaiting_review(ctx, unresolved_threads=1) is False

    def test_no_promote_without_successful_build(self):
        ctx = make_pr_context(labels={LABEL_AWAITING_CHANGES}, comment_lines=[])
        assert should_promote_to_awaiting_review(ctx, unresolved_threads=0) is False

    def test_no_promote_when_pr_check_blocked(self):
        ctx = make_pr_context(
            labels={LABEL_AWAITING_CHANGES, LABEL_PR_CHECK_BLOCKED},
            comment_lines=[f"Test build {BUILD_SUCCESS_COMMENT}"],
        )
        assert should_promote_to_awaiting_review(ctx, unresolved_threads=0) is False


class TestBuildComments:
    def test_review_comment_lists_all_reasons(self):
        comment = build_review_comment(["- reason one", "- reason two"])
        assert "reason one" in comment
        assert "reason two" in comment
        assert REVIEW_COMMENT_PARTIAL in comment
        assert "requirements" in comment

    def test_domain_comment_has_domain_and_url(self):
        comment = build_domain_comment("example.com")
        assert DOMAIN_COMMENT_PARTIAL in comment
        assert "example.com" in comment
        assert "org.flathub.VerifiedApps.txt" in comment


class TestHasMissingVideo:
    def test_video_link_on_same_line_is_not_missing(self):
        body = (
            "- [x] Please attach a video showcasing the application on Linux "
            "using the Flatpak. https://example.com/video.mp4\n"
        )
        assert has_missing_video(body, parse_checklist(body)) is False

    def test_video_link_on_next_line_is_not_missing(self):
        body = (
            "- [x] Please attach a video showcasing the application on Linux "
            "using the Flatpak.\nhttps://example.com/video.mp4\n"
        )
        assert has_missing_video(body, parse_checklist(body)) is False

    def test_no_link_after_item_is_missing(self):
        body = (
            "- [x] Please attach a video showcasing the application on Linux "
            "using the Flatpak.\n- [x] Some other item.\n"
        )
        assert has_missing_video(body, parse_checklist(body)) is True

    @pytest.mark.parametrize("marker", ["N/A", "n/a", "NA", "no video available"])
    def test_marked_na_is_missing(self, marker):
        body = (
            "- [x] Please attach a video showcasing the application on Linux "
            f"using the Flatpak. {marker}\n"
        )
        assert has_missing_video(body, parse_checklist(body)) is True

    def test_unchecked_video_item_with_link_is_still_missing(self):
        body = (
            "- [ ] Please attach a video showcasing the application on Linux "
            "using the Flatpak. https://example.com/video.mp4\n"
        )
        assert has_missing_video(body, parse_checklist(body)) is True

    def test_video_link_two_lines_down_with_blank_line_is_not_missing(self):
        body = (
            "- [x] Please attach a video showcasing the application on Linux "
            "using the Flatpak.\n"
            "\n"
            "https://github.com/user-attachments/assets/9f827b16-c627-4f47-8067-4ac0a108fcad\n"
        )
        assert has_missing_video(body, parse_checklist(body)) is False

    def test_link_three_lines_down_exceeds_lookahead_is_missing(self):
        body = (
            "- [x] Please attach a video showcasing the application on Linux "
            "using the Flatpak.\n"
            "\n"
            "\n"
            "https://example.com/video.mp4\n"
        )
        assert has_missing_video(body, parse_checklist(body)) is True

    def test_lookahead_stops_at_next_checklist_item(self):
        body = (
            "- [x] Please attach a video showcasing the application on Linux "
            "using the Flatpak.\n"
            "\n"
            "- [x] I am an author to the project. Link: https://example.com/issues/1\n"
        )
        assert has_missing_video(body, parse_checklist(body)) is True

    def test_video_item_absent_from_body_is_missing(self):
        assert (
            has_missing_video(NO_CHECKLIST_BODY, parse_checklist(NO_CHECKLIST_BODY))
            is True
        )


class TestValidatePR:
    def _validator(self, client) -> PRValidator:
        return PRValidator(client, "flathub/flathub")

    def test_valid_pr_triggers_build_and_awaiting_review(self):
        raw_pr = make_fake_raw_pr(
            title="Add com.example.foobar",
            body=FULL_CHECKLIST_BODY,
            files=["com.example.foobar.json", "cargo-sources.json"],
        )
        client = make_client(fetch_pr=raw_pr)
        validator = self._validator(client)

        assert validator.validate_pr(7378) is True
        comment_bodies = [c[0][1] for c in client.post_comment.call_args_list]
        assert any(BUILD_START_COMMENT_PARTIAL in b for b in comment_bodies)
        assert any(DOMAIN_COMMENT_PARTIAL in b for b in comment_bodies)
        client.add_labels.assert_any_call(7378, LABEL_AWAITING_REVIEW)

    def test_blocked_pr_gets_labelled_and_commented(self):
        raw_pr = make_fake_raw_pr(
            title="Not a valid title",
            body=FULL_CHECKLIST_BODY,
            files=["com.example.foobar.json"],
        )
        client = make_client(fetch_pr=raw_pr)
        validator = self._validator(client)

        assert validator.validate_pr(7378) is True
        client.add_labels.assert_any_call(7378, LABEL_PR_CHECK_BLOCKED)
        client.remove_labels.assert_any_call(7378, LABEL_AWAITING_REVIEW)
        assert REVIEW_COMMENT_PARTIAL in client.post_comment.call_args[0][1]

    def test_spam_pr_is_closed_with_comment(self):
        raw_pr = make_fake_raw_pr(
            title="Add com.example.foobar",
            body="Foobar",
            files=[".github/workflows/update_sources.yaml", "subdir/manifest.json"],
        )
        client = make_client(fetch_pr=raw_pr)
        validator = self._validator(client)

        assert validator.validate_pr(7378) is True
        client.close_pr.assert_called_once_with(7378)
        assert SPAM_CLOSE_COMMENT in client.post_comment.call_args[0][1]

    def test_undrafted_pr_loses_work_in_progress_label(self):
        raw_pr = make_fake_raw_pr(
            title="Add com.example.foobar",
            body=FULL_CHECKLIST_BODY,
            draft=False,
            files=["com.example.foobar.json"],
        )
        client = make_client(fetch_pr=raw_pr)
        self._validator(client).validate_pr(7378)
        client.remove_labels.assert_any_call(7378, LABEL_WORK_IN_PROGRESS)

    def test_no_build_when_blocked_label_present(self):
        raw_pr = make_fake_raw_pr(
            title="Add com.example.foobar",
            body=FULL_CHECKLIST_BODY,
            files=["com.example.foobar.json"],
            labels=[LABEL_BLOCKED],
        )
        client = make_client(fetch_pr=raw_pr)
        self._validator(client).validate_pr(7378)

        comment_bodies = [c[0][1] for c in client.post_comment.call_args_list]
        assert not any(BUILD_START_COMMENT_PARTIAL in b for b in comment_bodies)

    def test_demoted_when_unresolved_threads_present(self):
        raw_pr = make_fake_raw_pr(
            title="Add com.example.foobar",
            body=FULL_CHECKLIST_BODY,
            files=["com.example.foobar.json"],
            labels=[LABEL_AWAITING_REVIEW],
        )
        client = make_client(fetch_pr=raw_pr, unresolved_threads=2)
        self._validator(client).validate_pr(7378)

        client.add_labels.assert_any_call(7378, LABEL_AWAITING_CHANGES)
        client.remove_labels.assert_any_call(7378, LABEL_AWAITING_REVIEW)

    def test_review_comment_not_duplicated(self):
        raw_pr = make_fake_raw_pr(
            title="Not a valid title",
            body=FULL_CHECKLIST_BODY,
            files=["com.example.foobar.json"],
            comments=[
                FakeComment(
                    body=f"{REVIEW_COMMENT_PARTIAL} automated checks failed",
                    user=FakeUser(login="github-actions"),
                )
            ],
        )
        client = make_client(fetch_pr=raw_pr)
        self._validator(client).validate_pr(7378)

        comment_bodies = [c[0][1] for c in client.post_comment.call_args_list]
        assert not any(REVIEW_COMMENT_PARTIAL in b for b in comment_bodies)

    def test_false_when_pr_not_found(self):
        validator = self._validator(make_client(fetch_pr=None))
        assert validator.validate_pr(9999) is False

    def test_false_when_unresolved_threads_call_fails(self):
        raw_pr = make_fake_raw_pr(
            title="Add com.example.foobar",
            body=FULL_CHECKLIST_BODY,
            files=["com.example.foobar.json"],
        )
        client = make_client(fetch_pr=raw_pr)
        client.count_unresolved_review_threads.return_value = None
        assert self._validator(client).validate_pr(7378) is False

    def test_leave_open_label_skips_all_processing(self):
        raw_pr = make_fake_raw_pr(
            title="Not a valid title",
            body=NO_CHECKLIST_BODY,
            files=["subdir/file.json"],
            labels=[LABEL_LEAVE_OPEN],
        )
        client = make_client(fetch_pr=raw_pr)
        validator = self._validator(client)

        assert validator.validate_pr(7378) is True
        client.add_labels.assert_not_called()
        client.remove_labels.assert_not_called()
        client.post_comment.assert_not_called()
        client.close_pr.assert_not_called()
        client.count_unresolved_review_threads.assert_not_called()


class TestRun:
    def _validator(self, client) -> PRValidator:
        return PRValidator(client, "flathub/flathub")

    def test_draft_prs_get_work_in_progress(self):
        client = MagicMock()
        client.fetch_pr_numbers.side_effect = [[7402, 7405], [7403]]
        client.fetch_pull_request.return_value = None

        self._validator(client).run()
        client.add_labels.assert_any_call(7403, LABEL_WORK_IN_PROGRESS)

    def test_false_when_pr_fetch_fails(self):
        client = MagicMock()
        client.fetch_pr_numbers.return_value = None
        assert self._validator(client).run() is False

    def test_processes_all_non_draft_prs(self):
        raw_pr_a = make_fake_raw_pr(
            number=7402,
            title="Add com.example.alpha",
            body=FULL_CHECKLIST_BODY,
            files=["com.example.alpha.json"],
        )
        raw_pr_b = make_fake_raw_pr(
            number=7405,
            title="Add com.example.beta",
            body=FULL_CHECKLIST_BODY,
            files=["com.example.beta.json"],
        )

        client = MagicMock()
        client.fetch_pr_numbers.side_effect = [[7402, 7405], []]
        client.fetch_pull_request.side_effect = [raw_pr_a, raw_pr_b]
        client.count_unresolved_review_threads.return_value = 0
        client.add_labels.return_value = True
        client.remove_labels.return_value = True
        client.post_comment.return_value = True

        result = self._validator(client).run()
        assert result is True
        assert client.fetch_pull_request.call_count == 2

    def test_run_single_adds_work_in_progress_for_draft(self):
        raw_pr = make_fake_raw_pr(
            title="Add com.example.foobar",
            body=FULL_CHECKLIST_BODY,
            draft=True,
            files=["com.example.foobar.json"],
        )
        client = make_client(fetch_pr=raw_pr)
        self._validator(client).run_single(7378)
        client.add_labels.assert_any_call(7378, LABEL_WORK_IN_PROGRESS)
