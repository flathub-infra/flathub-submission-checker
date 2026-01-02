## Flathub submission checker

Internal Flathub submission checker action to aid review of new
submission pull requests.

### Usage

```yaml
name: Check PRs
on:
  workflow_dispatch:
  schedule:
    - cron: '0 */2 * * *'
  issue_comment:
    types: [created]

jobs:
  check-prs:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    permissions:
      pull-requests: write
    steps:
      - if: github.event_name == 'issue_comment' && contains(github.event.comment.body, '/review')
        uses: flathub-infra/submission-checker@main
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GH_REPO: ${{ github.repository }}
          COMMENT_BODY: ${{ github.event.comment.body }}
          PR_NUMBER: ${{ github.event.issue.number }}

      - if: github.event_name != 'issue_comment'
        uses: flathub-infra/submission-checker@main
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GH_REPO: ${{ github.repository }}
```

### Development

```sh
uv run ruff format
uv run ruff check --fix --exit-non-zero-on-fix
uv run mypy .
uv run pytest -vvv
```
