#!/usr/bin/env bash

set -e

# update CHANGELOG.md; use GITHUB_TOKEN ENV for GitHub authentication
git-cliff -o -v --github-repo "atticuszeller/sing-box-service"
# bump version and commit with tags
bump-my-version bump patch
# push remote
git push origin main --tags
