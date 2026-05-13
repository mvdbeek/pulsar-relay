# pulsar-relay release procedure.
#
# Each phase of cutting a release is one make target. Targets are
# idempotent where possible and abort early when their preconditions
# fail, so they can be re-run after fixing whatever broke.
#
# Full procedure:
#     make release-preflight
#     make release-bump      VERSION=0.2.0
#     make release-changelog VERSION=0.2.0
#     # (edit CHANGELOG.md to flesh out the stubbed section)
#     make release-pr        VERSION=0.2.0
#     # (review, wait for CI, squash-merge to main)
#     make release-tag-server VERSION=0.2.0
#     make release-tag-client VERSION=0.2.0
#     make release-verify    VERSION=0.2.0
#     make release-post      VERSION=0.2.0
#
# See .claude/skills/cut-release/SKILL.md for the human-in-the-loop
# checkpoints between phases.

.PHONY: release-preflight release-bump release-changelog release-pr release-tag-server release-tag-client release-verify release-post

# --- helpers ---------------------------------------------------------

# Guard targets that need VERSION=X.Y.Z. Aborts with a friendly message
# if VERSION is unset or malformed.
define require_version
	@if [ -z "$(VERSION)" ]; then \
		echo "ERROR: VERSION is required, e.g. make $@ VERSION=0.2.0" >&2; exit 2; \
	fi; \
	case "$(VERSION)" in \
		[0-9]*.[0-9]*.[0-9]*) ;; \
		*) echo "ERROR: VERSION must be semver X.Y.Z (got '$(VERSION)')" >&2; exit 2 ;; \
	esac
endef

# --- release procedure -----------------------------------------------

# 1. Confirm the repo is in a state where a release can be cut.
#    - working tree clean (no uncommitted changes)
#    - currently on main, OR on a release/* branch
#    - latest CI run on main is green (gh required)
release-preflight:
	@echo "==> Pre-flight checks"
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "ERROR: working tree is dirty; commit or stash first" >&2; \
		git status --short; exit 1; \
	fi
	@branch=$$(git branch --show-current); \
	case "$$branch" in \
		main|release/*) echo "  branch: $$branch (ok)" ;; \
		*) echo "ERROR: not on main or a release/* branch (got $$branch)" >&2; exit 1 ;; \
	esac
	@if command -v gh >/dev/null 2>&1; then \
		latest=$$(gh run list --branch main --limit 1 --json conclusion -q '.[0].conclusion' 2>/dev/null); \
		if [ "$$latest" = "success" ]; then \
			echo "  latest main CI: success"; \
		else \
			echo "WARN: latest main CI is '$$latest', not 'success'"; \
		fi; \
	else \
		echo "WARN: gh not on PATH; skipping CI freshness check"; \
	fi
	@echo "==> Pre-flight ok"

# 2. Bump the version in pyproject.toml + client/pyproject.toml in
#    lockstep. Both packages release on the same VERSION; review the
#    diff before committing.
release-bump:
	$(call require_version)
	@echo "==> Bumping pyproject.toml + client/pyproject.toml to $(VERSION)"
	@if ! grep -q '^version = ' pyproject.toml || ! grep -q '^version = ' client/pyproject.toml; then \
		echo "ERROR: 'version = ' line not found in one of the pyprojects" >&2; exit 1; \
	fi
	@perl -i -pe 's/^version = ".*"$$/version = "$(VERSION)"/' pyproject.toml client/pyproject.toml
	@echo "  server pyproject.toml: $$(grep '^version = ' pyproject.toml)"
	@echo "  client pyproject.toml: $$(grep '^version = ' client/pyproject.toml)"

# 3. Insert an empty ``## [$(VERSION)] - YYYY-MM-DD`` stub above the
#    most recent version in CHANGELOG.md. The caller fills in the
#    ``### Server`` and ``### Client`` subsections by hand before
#    opening the release-prep PR. Re-running is safe — it detects an
#    existing section and exits with a message instead of duplicating.
release-changelog:
	$(call require_version)
	@set -e; \
	if [ ! -f CHANGELOG.md ]; then \
		echo "ERROR: CHANGELOG.md missing — create it first" >&2; exit 1; \
	fi; \
	if grep -q "^## \[$(VERSION)\]" CHANGELOG.md; then \
		echo "  [$(VERSION)] section already present — nothing to do"; exit 0; \
	fi; \
	today=$$(date -u +%Y-%m-%d); \
	tmp=$$(mktemp); \
	printf '## [%s] - %s\n\n### Server\n\n### Client (`pulsar-relay-client`)\n\n' "$(VERSION)" "$$today" > "$$tmp.stub"; \
	awk -v stubfile="$$tmp.stub" ' \
		function flush_stub() { while ((getline l < stubfile) > 0) print l; close(stubfile); inserted=1 } \
		BEGIN { inserted=0 } \
		/^## \[Unreleased\]/ && !inserted { print; print ""; flush_stub(); next } \
		!inserted && /^## \[/ { flush_stub() } \
		{ print } \
	' CHANGELOG.md > "$$tmp" && mv "$$tmp" CHANGELOG.md; \
	rm -f "$$tmp.stub"; \
	echo "==> Stubbed [$(VERSION)] section in CHANGELOG.md — fill in ### Server / ### Client now"

# 5. Open the release-prep PR. Assumes the working tree already
#    contains the version bumps + curated CHANGELOG.md + any
#    workflow / Makefile / skill changes. Commits, pushes, and opens
#    a PR against ``main`` via ``gh``. The caller must squash-merge
#    after CI goes green; tags later point at the merge SHA.
release-pr:
	$(call require_version)
	@set -e; \
	branch=$$(git branch --show-current); \
	if [ "$$branch" != "release/$(VERSION)" ]; then \
		echo "ERROR: must be on release/$(VERSION) (current: $$branch)" >&2; exit 1; \
	fi; \
	if [ -z "$$(git status --porcelain)" ] && git diff --quiet HEAD~0..HEAD -- pyproject.toml 2>/dev/null; then \
		echo "ERROR: nothing to commit — did release-bump run?" >&2; exit 1; \
	fi; \
	git add -A; \
	git commit -m "Release $(VERSION)" || true; \
	git push -u origin "release/$(VERSION)"; \
	gh pr create --base main --head "release/$(VERSION)" \
		--title "Release $(VERSION)" \
		--body "Cuts pulsar-relay $(VERSION) (server + client in lockstep). See CHANGELOG.md for the curated release notes."
	@echo "==> PR opened — wait for CI to go green, then squash-merge to main"

# 6a. Tag the server release. Run AFTER the release-prep PR is
#     squash-merged to main; tagging the merge SHA ensures
#     ``release.yml`` builds against the same tree the PR shipped.
release-tag-server:
	$(call require_version)
	@set -e; \
	branch=$$(git branch --show-current); \
	if [ "$$branch" != "main" ]; then \
		echo "ERROR: switch to main (and pull) before tagging (current: $$branch)" >&2; exit 1; \
	fi; \
	git fetch origin main >/dev/null 2>&1 || true; \
	if [ "$$(git rev-parse HEAD)" != "$$(git rev-parse origin/main)" ]; then \
		echo "ERROR: local main is not at origin/main — git pull first" >&2; exit 1; \
	fi; \
	pkg_version=$$(grep '^version = ' pyproject.toml | cut -d'"' -f2); \
	if [ "$$pkg_version" != "$(VERSION)" ]; then \
		echo "ERROR: pyproject.toml version is $$pkg_version, not $(VERSION) — release-prep PR not merged?" >&2; exit 1; \
	fi; \
	if ! grep -q "^## \[$(VERSION)\]" CHANGELOG.md; then \
		echo "ERROR: CHANGELOG.md has no [$(VERSION)] section" >&2; exit 1; \
	fi; \
	if git rev-parse "v$(VERSION)" >/dev/null 2>&1; then \
		echo "ERROR: tag v$(VERSION) already exists" >&2; exit 1; \
	fi; \
	git tag -a "v$(VERSION)" -m "Release v$(VERSION) (server)"; \
	git push origin "v$(VERSION)"
	@echo "==> Tag v$(VERSION) pushed — waiting for release.yml run to register"
	@# Poll until the new run (keyed on the tag's headBranch) shows up
	@# in the API. ``gh run list --limit 1`` immediately after a push
	@# can return the previous run (queued ones aren't indexed yet).
	@set -e; \
	for _ in 1 2 3 4 5 6 7 8 9 10; do \
		run_id=$$(gh run list --workflow=release.yml --branch "v$(VERSION)" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null); \
		if [ -n "$$run_id" ]; then break; fi; \
		sleep 3; \
	done; \
	if [ -z "$$run_id" ]; then echo "ERROR: could not find release.yml run for v$(VERSION)" >&2; exit 1; fi; \
	echo "==> tailing run $$run_id"; \
	gh run watch --exit-status "$$run_id" || \
		(echo "release.yml failed — investigate with 'gh run view --log-failed $$run_id'" >&2; exit 1)

# 6b. Tag the client release. Same shape; fires release-client.yml.
release-tag-client:
	$(call require_version)
	@set -e; \
	branch=$$(git branch --show-current); \
	if [ "$$branch" != "main" ]; then \
		echo "ERROR: switch to main before tagging (current: $$branch)" >&2; exit 1; \
	fi; \
	pkg_version=$$(grep '^version = ' client/pyproject.toml | cut -d'"' -f2); \
	if [ "$$pkg_version" != "$(VERSION)" ]; then \
		echo "ERROR: client/pyproject.toml version is $$pkg_version, not $(VERSION)" >&2; exit 1; \
	fi; \
	if git rev-parse "client-v$(VERSION)" >/dev/null 2>&1; then \
		echo "ERROR: tag client-v$(VERSION) already exists" >&2; exit 1; \
	fi; \
	git tag -a "client-v$(VERSION)" -m "Release client-v$(VERSION)"; \
	git push origin "client-v$(VERSION)"
	@echo "==> Tag client-v$(VERSION) pushed — waiting for release-client.yml run to register"
	@set -e; \
	for _ in 1 2 3 4 5 6 7 8 9 10; do \
		run_id=$$(gh run list --workflow=release-client.yml --branch "client-v$(VERSION)" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null); \
		if [ -n "$$run_id" ]; then break; fi; \
		sleep 3; \
	done; \
	if [ -z "$$run_id" ]; then echo "ERROR: could not find release-client.yml run for client-v$(VERSION)" >&2; exit 1; fi; \
	echo "==> tailing run $$run_id"; \
	gh run watch --exit-status "$$run_id" || \
		(echo "release-client.yml failed — investigate with 'gh run view --log-failed $$run_id'" >&2; exit 1)

# 7. Post-release verification. Checks both PyPI versions, the
#    ghcr.io image, and both GitHub Release pages. Idempotent /
#    read-only — safe to re-run.
release-verify:
	$(call require_version)
	@set -e; \
	echo "==> PyPI: pulsar-relay"; \
	curl -sf "https://pypi.org/pypi/pulsar-relay/$(VERSION)/json" | jq -r '.info.version' \
		|| { echo "ERROR: pulsar-relay $(VERSION) not found on PyPI" >&2; exit 1; }; \
	echo "==> PyPI: pulsar-relay-client"; \
	curl -sf "https://pypi.org/pypi/pulsar-relay-client/$(VERSION)/json" | jq -r '.info.version' \
		|| { echo "ERROR: pulsar-relay-client $(VERSION) not found on PyPI" >&2; exit 1; }; \
	echo "==> ghcr.io image"; \
	ghcr_token=$$(curl -sf "https://ghcr.io/token?scope=repository:mvdbeek/pulsar-relay:pull" | jq -r '.token'); \
	manifest_status=$$(curl -sf -o /dev/null -w '%{http_code}' \
		-H "Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json" \
		-H "Authorization: Bearer $$ghcr_token" \
		"https://ghcr.io/v2/mvdbeek/pulsar-relay/manifests/$(VERSION)" || echo 000); \
	if [ "$$manifest_status" != "200" ]; then \
		echo "ERROR: ghcr.io/mvdbeek/pulsar-relay:$(VERSION) returned HTTP $$manifest_status" >&2; exit 1; \
	fi; \
	echo "  manifest present (HTTP 200)"; \
	echo "==> GitHub Release v$(VERSION)"; \
	gh release view "v$(VERSION)" --json name -q '.name'; \
	echo "==> GitHub Release client-v$(VERSION)"; \
	gh release view "client-v$(VERSION)" --json name -q '.name'
	@echo "==> Release $(VERSION) verified"

# 8. Re-add the ``## [Unreleased]`` placeholder above [VERSION] so the
#    next release cycle has a stub to grow into. Idempotent.
release-post:
	$(call require_version)
	@set -e; \
	if grep -q "^## \[Unreleased\]" CHANGELOG.md; then \
		echo "  [Unreleased] block already present — nothing to do"; exit 0; \
	fi; \
	tmp=$$(mktemp); \
	awk -v ver="$(VERSION)" ' \
		!inserted && $$0 ~ "^## \\[" ver "\\]" { print "## [Unreleased]"; print ""; inserted=1 } \
		{ print } \
	' CHANGELOG.md > "$$tmp" && mv "$$tmp" CHANGELOG.md
	@echo "==> Added [Unreleased] placeholder — commit + push to main when ready"
