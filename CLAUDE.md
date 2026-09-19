# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Versioning

`pyproject.toml`'s `version` field and the actual released version are two separate things: `docker-release.yml`'s `build-and-push` job derives the shipped version from `git describe --tags` (auto-incrementing the patch component from the latest `vX.Y.Z` tag), not from `pyproject.toml`. Before bumping `pyproject.toml`'s version for a change, check the latest real release tag first (`git tag --sort=-v:refname | head -1`) rather than just incrementing whatever value currently sits in `pyproject.toml` — if that value is already ahead of the latest tag (e.g. a previous PR in the same release cycle bumped it but nothing was tagged/released yet), fold the new change into that same still-unreleased version instead of bumping again, to avoid a phantom intermediate version that never shipped. See README.md's "🏷️ Versionierung" section for the full release/tagging scheme, including the reserved 4th version digit for OS-patch-only releases.
