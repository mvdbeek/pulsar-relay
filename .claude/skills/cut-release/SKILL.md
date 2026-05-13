---
name: cut-release
description: Cut a coordinated pulsar-relay server + client release. Bumps versions in both pyproject.toml files, drafts the CHANGELOG.md entry, validates locally, opens the release-prep PR, then tags both server (v$VERSION) and client (client-v$VERSION) to fire the PyPI + ghcr.io workflows. Use when the user asks to "cut a release" or "release X.Y.Z".
---

# Cut a pulsar-relay release

The release procedure is encoded in the top-level `Makefile` — every phase is one `make` target. This skill sequences the targets and lists the human-in-the-loop checkpoints between them.

## Inputs

- `VERSION` (required): semver `X.Y.Z`. Used as the server tag (`v$VERSION`) and the client tag (`client-v$VERSION`). The two packages are versioned in lockstep.

## Procedure

1. **Pre-flight** — `make release-preflight`. Asserts a clean tree, that you're on `main` or a `release/*` branch, and that the last CI run on `main` was green. **Stop here if pre-flight fails.**

2. **Create the release branch** — `git checkout -b release/$VERSION`. The next steps modify tracked files; doing this on `main` directly would mean force-pushing later.

3. **Bump versions** — `make release-bump VERSION=$VERSION`. Edits `pyproject.toml` and `client/pyproject.toml` in place. Verify with `git diff`.

4. **Stub the CHANGELOG entry** — `make release-changelog VERSION=$VERSION`. Inserts an empty `## [$VERSION] - YYYY-MM-DD` section above the latest one in `CHANGELOG.md`. **Then pause: hand-fill the `### Server` and `### Client` subsections** by reviewing the merged PRs since the last tag (`git log $(git describe --tags --abbrev=0 main)..main --oneline`). Use Keep-a-Changelog headings (`### Added`, `### Changed`, `### Fixed`, `### Removed`, `### Security`).

5. **Open the release-prep PR** — `make release-pr VERSION=$VERSION`. Pushes the branch and opens a PR via `gh`. **Pause until CI is green and the user squash-merges.**

6. **Tag the server** — once `main` has the merge, `make release-tag-server VERSION=$VERSION`. Creates `v$VERSION` (annotated), pushes it, and tails the `Release` workflow via `gh run watch`. The workflow publishes to PyPI (trusted publishing), builds + pushes the multi-arch Docker image to `ghcr.io/mvdbeek/pulsar-relay`, and creates the GitHub Release using the curated CHANGELOG section.

7. **Tag the client** — `make release-tag-client VERSION=$VERSION`. Same shape, fires `release-client.yml`, publishes `pulsar-relay-client` to PyPI, creates the GitHub Release. Done in a second step so the two workflow runs are easy to read separately.

8. **Verify** — `make release-verify VERSION=$VERSION`. Checks both PyPI versions, the Docker manifest, and the two GitHub Release pages. If anything is missing, do not retry blindly — investigate the failed workflow first (`gh run view`), because PyPI's trusted publishing does not allow republishing the same version.

9. **Re-add `[Unreleased]`** — `make release-post VERSION=$VERSION`. Inserts a fresh `## [Unreleased]` block above `## [$VERSION]` so the next release has a stub to grow into. Open a tiny PR or push directly to `main`.

## Human-in-the-loop checkpoints

These steps **must** wait for human review:

- Between step 4 and step 5: the user (or Claude with the user watching) writes the CHANGELOG content.
- Between step 5 and step 6: the user reviews the PR and squash-merges it. **Tags must point at the merge commit on `main`, not at the release branch tip.**
- Between step 7 and step 8: optional — wait a minute or two for PyPI's CDN to propagate before verifying.

## Failure modes worth knowing

- **PyPI says the version already exists.** Trusted publishing in `release.yml` is irreversible. If the workflow failed *after* the PyPI step, do not bump the patch — investigate and decide between yanking and republishing as the next patch.
- **Docker tag pushed but PyPI failed.** Re-running the workflow re-pushes the Docker image (idempotent) but cannot republish the PyPI artifact. May need to yank + bump.
- **Tag pushed to wrong commit.** `git push --delete origin v$VERSION` then re-tag. The workflow run from the bad tag has likely already started; cancel it with `gh run cancel`.
- **CHANGELOG section is empty / wrong format.** The `Extract release notes` step in the workflow `::error::`s out before creating the GitHub Release. Fix the CHANGELOG on `main`, force-push the tag back to that commit.

## Critical files

- `Makefile` — targets for each step.
- `CHANGELOG.md` — Keep-a-Changelog format, shared by both packages with `### Server` / `### Client` subsections.
- `pyproject.toml` and `client/pyproject.toml` — versions in lockstep.
- `.github/workflows/release.yml` and `.github/workflows/release-client.yml` — triggered by tags, extract the matching `[$VERSION]` block from `CHANGELOG.md`.
