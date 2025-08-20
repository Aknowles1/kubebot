# KubePolicy PR Bot

Docker-based GitHub Action that scans changed Kubernetes YAML manifests on pull requests and enforces baseline security and hygiene policies. It emits GitHub Annotations for each finding and can post a single PR comment with a summary and suggested YAML patches.

## Features
- Detects changed `*.yml`/`*.yaml` files in PRs (via `git diff`).
- Parses multi-document YAML, supports Pod, Deployment, Job, CronJob templates.
- Emits `::error`/`::warning` annotations with file context.
- Optional single PR comment with summary and patch suggestions.
- Configurable severity threshold and file include/exclude globs.
- Runs entirely in CI (no external services).

## Policies

Errors (fail the job):
- `securityContext.privileged: true` (container-level)
- `hostNetwork`, `hostPID`, or `hostIPC` set to `true` (pod spec)
- Container image uses `:latest` or has no tag (digests allowed)
- Containers missing both `resources.requests` and `resources.limits`
- `capabilities.add` includes any of: `SYS_ADMIN`, `NET_ADMIN`, `SYS_PTRACE`, `DAC_READ_SEARCH`
- `hostPath` volumes mounted without `readOnly: true`

Warnings (do not fail unless `severity_threshold: warning`):
- Missing `runAsNonRoot: true` (pod or container securityContext)
- Missing `readOnlyRootFilesystem: true` (container securityContext)
- Missing `seccompProfile.type: RuntimeDefault` (pod or container securityContext)
- Missing `livenessProbe` or `readinessProbe`

## Inputs
- `severity_threshold` (default `error`): set to `warning` to fail on warnings too.
- `include_glob` (default `**/*.yml,**/*.yaml`): comma-separated globs to include.
- `exclude_glob` (default empty): comma-separated globs to exclude.
- `post_pr_comment` (default `true`): set `false` to skip the PR summary comment.
- `github_token` (optional): token used to post the PR comment (use `${{ secrets.GITHUB_TOKEN }}`).

## Usage
Add a workflow similar to the example below. Ensure `actions/checkout` uses `fetch-depth: 0` for reliable diffs.

```yaml
name: KubePolicy PR Bot

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  kubepolicy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: KubePolicy PR Bot
        uses: ./  # or owner/repo@v1
        with:
          severity_threshold: error
          include_glob: "**/*.yml,**/*.yaml"
          exclude_glob: ""
          post_pr_comment: true
          github_token: "${{ secrets.GITHUB_TOKEN }}"
```

## Sample Manifests
To see findings quickly, open a PR that adds or changes the sample manifests under `samples/`.

- `samples/bad/deployment-insecure.yaml` — intentionally violates multiple error and warning rules.
- `samples/good/deployment-secure.yaml` — a secure baseline that should pass.

You can also point `include_glob` to `samples/**/*.yaml` while testing.

## Output
- GitHub Annotations per file and finding (appears in the PR Checks and file diffs).
- Optional single PR comment with a summary and suggested patches for common issues (e.g., pin image tags, add resources, enable seccomp, etc.).

## Notes
- Line numbers in annotations default to 1 due to YAML parsing without location metadata.
- The action computes changed files via `git diff origin/<base>...HEAD`; if that fails, it falls back to merge-base and, as a last resort, scans the repo.

## Development
- Docker image defined in `Dockerfile`; Python entrypoint is `src/main.py`.
- Dependencies in `requirements.txt`.

---
Made with care to be strict by default but configurable for your workflow.
