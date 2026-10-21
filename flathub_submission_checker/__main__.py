import argparse
import logging
import os

from github.GithubException import GithubException

from flathub_submission_checker import __version__
from flathub_submission_checker.github_client import PyGithubClient
from flathub_submission_checker.validator import PRValidator

logger = logging.getLogger(__name__)


def _build_client(gh_token: str, gh_repo: str) -> PyGithubClient | None:
    try:
        return PyGithubClient(gh_token, gh_repo)
    except GithubException as err:
        logger.error("Failed to initialise GitHub for %r: %s", gh_repo, err)
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flathub submission checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        usage=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--help",
        action="help",
        help="Show this help message and exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show the version and exit",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")

    parse_args()

    try:
        gh_token = os.environ["GH_TOKEN"]
        gh_repo = os.environ["GH_REPO"]
    except KeyError as err:
        logger.error("Missing GH_TOKEN or GH_REPO: %s", err)
        return 1

    client = _build_client(gh_token, gh_repo)
    if client is None:
        return 1

    validator = PRValidator(client, gh_repo)

    return 0 if validator.run() else 1


if __name__ == "__main__":
    raise SystemExit(main())
