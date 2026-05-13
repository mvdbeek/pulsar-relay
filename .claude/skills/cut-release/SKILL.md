---
name: cut-release
description: Cut a pulsar-relay server and/or pulsar-relay-client release. Drives the top-level Makefile (release-* targets) to bump the relevant pyproject(s), draft the CHANGELOG entry, open the release-prep PR, then fire the matching tag(s) to publish via PyPI trusted publishing and (for the server) push the multi-arch image to ghcr.io. Supports both lockstep releases (server + client at the same version) and component-only releases. Use when the user asks to "cut a release", "release X.Y.Z", or "ship a server/client patch".
---

# Cut a pulsar-relay release

The release procedure is encoded in the top-level `Makefile` — every phase is one `make` target. This skill sequences the targets and lists the human-in-the-loop checkpoints between them.

## Inputs

- `VERSION` (required): semver `X.Y.Z`.
- **Shape**: pick one before starting.
  - **Lockstep** — both server and client ship at this VERSION. Use this when the change touches both halves (typical for breaking-change cycles).
  - **Component-only** — only the server *or* only the client gets a new tag. The other stays where it is. Versions for the two packages can and will diverge over time; that's intentional.

## Lockstep procedure

1. **Pre-flight** — `make release-preflight`. Asserts a clean tree, that you're on `main` or a `release/*` branch, and that the last CI run on `main` was green. **Stop if pre-flight fails.**

2. **Create the release branch** — `git checkout -b release/$VERSION`.

3. **Bump both versions** — `make release-bump VERSION=$VERSION`. Edits `pyproject.toml`, `client/pyproject.toml`, and `client/pulsar_relay_client/__version__`. Verify with `git diff`.

4. **Stub the CHANGELOG entry** — `make release-changelog VERSION=$VERSION` inserts an empty `## [$VERSION] - YYYY-MM-DD` section with both `### Server` and `### Client (...)` subsections. **Pause and hand-fill** by reviewing the merged PRs since the last tag (`git log $(git describe --tags --abbrev=0 main)..main --oneline`). Use Keep-a-Changelog headings (`### Added`, `### Changed`, `### Fixed`, `### Removed`, `### Security`).

5. **Open the release-prep PR** — `make release-pr VERSION=$VERSION`. **Pause until CI is green and the user squash-merges.**

6. **Tag the server** — `make release-tag-server VERSION=$VERSION`. Fires `release.yml`: PyPI publish + multi-arch image to `ghcr.io/mvdbeek/pulsar-relay` + GitHub Release using the curated CHANGELOG section.

7. **Tag the client** — `make release-tag-client VERSION=$VERSION`. Fires `release-client.yml`: PyPI publish + GitHub Release. Done in a second step so the two workflow runs are easy to read.

8. **Verify** — `make release-verify VERSION=$VERSION`. Checks both PyPI versions, the Docker manifest, and both GitHub Release pages.

9. **Re-add `[Unreleased]`** — `make release-post VERSION=$VERSION`, then commit + push to `main`.

## Component-only procedure

The Makefile splits every per-component step into a `-server` / `-client` variant. Use them when only one package has changes.

### Client-only release

```
make release-preflight
make release-bump-client      VERSION=$VERSION
make release-changelog-client VERSION=$VERSION
# (fill in ### Client subsection)
make release-pr               VERSION=$VERSION
# (merge)
make release-tag-client       VERSION=$VERSION
make release-verify-client    VERSION=$VERSION
make release-post             VERSION=$VERSION
```

Server pyproject is untouched. The `## [$VERSION]` block in CHANGELOG.md contains only `### Client (...)`. No `v$VERSION` tag is created.

### Server-only release

```
make release-preflight
make release-bump-server      VERSION=$VERSION
make release-changelog-server VERSION=$VERSION
# (fill in ### Server subsection)
make release-pr               VERSION=$VERSION
# (merge)
make release-tag-server       VERSION=$VERSION
make release-verify-server    VERSION=$VERSION
make release-post             VERSION=$VERSION
```

Client pyproject is untouched. No `client-v$VERSION` tag is created.

### Picking the right VERSION for a component-only release

The server and client have independent version histories. Bump the component being shipped past *its own* most recent tag — not the other component's. e.g. if server is at `0.3.0` and client is at `0.3.1`, a client patch goes to `0.3.2` and a server patch goes to `0.3.1` (yes, the two packages can share a version number on adjacent releases; that's fine).

## Human-in-the-loop checkpoints

These steps **must** wait for human review:

- Between changelog stub and `release-pr`: the user (or Claude with the user watching) writes the CHANGELOG content.
- Between `release-pr` and `release-tag-*`: the user reviews the PR and squash-merges it. **Tags must point at the merge commit on `main`, not at the release branch tip.**
- Between tag and verify: optional — wait a minute or two for PyPI's CDN to propagate.

## Failure modes worth knowing

- **PyPI says the version already exists.** Trusted publishing is irreversible. If the workflow failed *after* the PyPI step, do not bump the patch — investigate and decide between yanking and republishing as the next patch.
- **Docker tag pushed but PyPI failed (server only).** Re-running the workflow re-pushes the Docker image (idempotent) but cannot republish the PyPI artifact. May need to yank + bump.
- **Tag pushed to wrong commit.** `git push --delete origin v$VERSION` (or `client-v$VERSION`) then re-tag. The workflow run from the bad tag has likely already started; cancel it with `gh run cancel`.
- **CHANGELOG section is empty / wrong format.** The `Extract release notes` step in the workflow `::error::`s out before creating the GitHub Release. Fix the CHANGELOG on `main`, force-push the tag back to that commit.
- **`release-tag-server` complains about version mismatch.** The error message includes the right next step — typically you wanted `release-tag-client` (the workflow refuses to publish a server release whose `pyproject.toml` doesn't match the tag).

## Critical files

- `Makefile` — targets for each step (per-component variants + lockstep wrappers).
- `CHANGELOG.md` — Keep-a-Changelog format. Sections key on the bumped component(s): a client-only release has only `### Client (...)`; a server-only release has only `### Server`; a lockstep release has both.
- `pyproject.toml` and `client/pyproject.toml` — versions are *independent*; they happen to match during lockstep cycles.
- `.github/workflows/release.yml` — triggered by `v*.*.*` tags, extracts `[VERSION]` block from CHANGELOG.md.
- `.github/workflows/release-client.yml` — triggered by `client-v*.*.*` tags, extracts the same block (so client-only entries should still be marked `### Client (...)` so consumers know which package the body describes).
