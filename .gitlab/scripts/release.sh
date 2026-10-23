#!/usr/bin/env sh
# Keyless semantic release for the default branch.
#
# Reads the Conventional Commits since the last v* tag, decides the bump (feat -> minor, fix/perf -> patch,
# "BREAKING CHANGE" or "type!:" -> major), builds the release notes from the "Unreleased" section the Release
# Scribe flow wrote into CHANGELOG.md, and creates the tag + GitLab Release through the Releases API using the
# job's own short-lived CI_JOB_TOKEN. No project access token, no bot user, nothing to rotate.
#
# Exits 0 without releasing when nothing releasable landed (docs/ci/chore only).
set -eu

LAST=$(git describe --tags --abbrev=0 --match 'v*' 2>/dev/null || true)
RANGE=${LAST:+$LAST..}HEAD
BASE=${LAST#v}; BASE=${BASE:-1.0.0}                     # CHANGELOG starts at 1.0.0, released before tags existed

MAJOR=$(echo "$BASE" | cut -d. -f1); MINOR=$(echo "$BASE" | cut -d. -f2); PATCH=$(echo "$BASE" | cut -d. -f3)
BUMP=none
git log --no-merges --format='%s%n%b' "$RANGE" > /tmp/commits.txt
if grep -Eq '^[a-z]+(\([^)]*\))?!:|^BREAKING CHANGE:' /tmp/commits.txt; then BUMP=major
elif grep -Eq '^feat(\([^)]*\))?:' /tmp/commits.txt; then BUMP=minor
elif grep -Eq '^(fix|perf)(\([^)]*\))?:' /tmp/commits.txt; then BUMP=patch
fi
case "$BUMP" in
  major) MAJOR=$((MAJOR+1)); MINOR=0; PATCH=0 ;;
  minor) MINOR=$((MINOR+1)); PATCH=0 ;;
  patch) PATCH=$((PATCH+1)) ;;
  none)  echo "No feat/fix/perf commit since ${LAST:-the first commit}; nothing to release."; echo "RELEASE_TAG=" > release.env; exit 0 ;;
esac
VERSION="$MAJOR.$MINOR.$PATCH"; TAG="v$VERSION"
echo "Releasing $TAG ($BUMP bump; last tag: ${LAST:-none})"

# ---- notes: CHANGELOG "Unreleased" section (written by the Release Scribe flow), then the facts auditors ask for
NOTES=/tmp/notes.md
awk '/^## \[?Unreleased/{f=1; next} /^## /{f=0} f' CHANGELOG.md > $NOTES || true
if [ ! -s $NOTES ]; then
  { echo "### Changes"; git log --no-merges --pretty='- %s (%h)' "$RANGE"; } > $NOTES
fi
POLICY_VERSION=$(grep -E '^[[:space:]]*version:' config/policy.yaml | head -n1 | sed -E 's/.*version:[[:space:]]*"?([^"]*)"?.*/\1/')
{
  echo
  echo "### Release facts"
  echo "- Image: \`$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA\` (also tagged \`$TAG\` by the tag pipeline)"
  echo "- Fraud policy: v${POLICY_VERSION:-?} (\`config/policy.yaml\`; the 20 benchmark decisions reproduced in this pipeline)"
  echo "- Commits: $(git rev-list --no-merges --count "$RANGE") since ${LAST:-the first commit}"
  echo "- Pipeline: $CI_PIPELINE_URL"
} >> $NOTES

# ---- create tag + release with the job token (Releases API); the tag is created from this commit
PAYLOAD=$(jq -n --arg tag "$TAG" --arg ref "$CI_COMMIT_SHA" --arg name "SentinelGraph $TAG" --rawfile notes $NOTES \
  '{tag_name:$tag, ref:$ref, name:$name, description:$notes, assets:{links:[]}}')
CODE=$(curl -sS -o /tmp/release.json -w '%{http_code}' -X POST \
  -H "JOB-TOKEN: $CI_JOB_TOKEN" -H "Content-Type: application/json" \
  "$CI_API_V4_URL/projects/$CI_PROJECT_ID/releases" --data "$PAYLOAD")
if [ "$CODE" != "201" ]; then echo "Release API returned $CODE:"; cat /tmp/release.json; exit 1; fi
echo "Created release $(jq -r '.tag_name' /tmp/release.json): $(jq -r '._links.self' /tmp/release.json)"
echo "RELEASE_TAG=$TAG" > release.env
