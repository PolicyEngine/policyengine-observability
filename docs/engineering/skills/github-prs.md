# GitHub PRs

These rules apply to every developer and AI agent opening pull requests in this
repository.

## Same-Repository PRs

Open PRs from branches in `PolicyEngine/policyengine-observability`, not from
personal forks. Fork PRs are more likely to miss repository secrets and can
produce different CI behavior from branches in the canonical repository.

Before creating or sharing a PR:

1. Confirm the canonical repository is reachable:
   `gh repo view PolicyEngine/policyengine-observability --json nameWithOwner`.
2. Open a GitHub issue for the work, or verify that an appropriate issue
   already exists. Every PR must have an accompanying issue.
3. Put exactly `Fixes #ISSUE_NUMBER` as the first line of the PR description,
   using the issue number from the issue created or found in the previous step.
   This line must be the first content in the description so GitHub auto-closes
   the issue when the PR merges.
4. Add a Towncrier changelog fragment under `changelog.d/` using the issue
   number or a clear slug and the appropriate configured type, for example
   `changelog.d/ISSUE_NUMBER.fixed.md`.
5. Before the final commit in the PR, run the formatter and linter:
   `uv run --extra dev ruff format .` and
   `uv run --extra dev ruff check .`. If formatting changes files, review and
   stage those changes before committing.
6. Run tests with coverage:
   `uv run --extra dev --extra all coverage run -m pytest` and
   `uv run --extra dev --extra all coverage report`.
7. Push the current branch to the canonical repository:
   `git push origin HEAD`.
8. Create the PR as a draft from that same repository:
   `gh pr create --draft --repo PolicyEngine/policyengine-observability --head "$(git branch --show-current)" --base main`.
9. Verify the PR is draft and the head repository is canonical:
   `gh pr view <PR> --repo PolicyEngine/policyengine-observability --json isDraft,headRepositoryOwner,headRepository`.
10. Before sharing the PR, verify CI has been checked:
    `gh pr checks <PR> --repo PolicyEngine/policyengine-observability`.

If you cannot push to the canonical repository, stop and ask for access. Do not
create a fork PR as a fallback. If you accidentally create one, close it and
replace it with a same-repository draft PR.

## PR Title

Do not add `[codex]`, `[claude]`, `[copilot]`, or other agent labels to PR
titles. Use a plain descriptive title.
