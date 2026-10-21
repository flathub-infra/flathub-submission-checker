## Flathub submission checker

Flathub submission checker action to aid review of new
submission pull requests.

### Usage

```yaml
name: Check PRs
on:
  workflow_dispatch:
  schedule:
    - cron: '0 */2 * * *'

jobs:
  check-prs:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    permissions:
      pull-requests: write
    steps:
      - uses: flathub-infra/submission-checker@main
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GH_REPO: ${{ github.repository }}
```

### Development

```sh
uv sync --all-extras --all-groups --frozen --allow-python-downloads
uv run ruff format
uv run ruff check --fix --exit-non-zero-on-fix
uv run mypy .
uv run pytest -vvv
```
