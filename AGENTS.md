# Agent Instructions

These instructions apply repository-wide.

## Skills System

Canonical AI-facing engineering skills live under `docs/engineering/skills/`.
Use those files as the source of truth across Codex, Claude, Copilot, and other
AI tools.

Before opening, replacing, or sharing any pull request, read
`docs/engineering/skills/github-prs.md`.

Before making or reviewing repository-wide API, testing, documentation, release,
or package-boundary changes, read
`docs/engineering/skills/repository-guidance.md`.

## Repository Boundaries

`policyengine-observability` is the shared PolicyEngine observability runtime.
Keep framework-specific behavior in adapters or integrations, keep the core
runtime usable outside HTTP requests, and keep observability failures from
breaking application code.
