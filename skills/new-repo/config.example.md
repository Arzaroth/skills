# new-repo config

Copy to `~/.config/skills/new-repo/config.md` and fill it in.

## Where repos live

- Local: `~/repos/<name>`
- Default branch: `main`
- Commit subjects: conventional commits. Trailers: whatever the system reminder says.

## Hosts

- Source of truth: GitHub, owner `my-user`, CLI `gh`
- Mirror: none
<!-- Or a self-hosted forge as source of truth with GitHub as a push mirror:
- Source of truth: Forgejo at `git.example.com` (SSH `ssh://git@git.example.com/<owner>/<name>.git`), owner `my-user`, CLI `tea`
- Mirror: GitHub, owner `my-user`, CLI `gh`
- Mirror method: Forgejo server-side SSH push mirror with one GitHub deploy key per repo, interval `8h0m0s`, `sync_on_commit: true`
-->

## Families

Decides identity, CI host and release host. One family is fine.

- *Default*: global git identity, CI and Releases on GitHub Actions.

## Sibling templates

| Stack | Closest sibling |
| --- | --- |
| Rust CLI | `my-rust-tool` (workspace, coverage gate, release workflow) |
| TypeScript app | `my-web-app` (pnpm workspace) |

## Licences

- MIT (my-rust-tool)
- Apache-2.0 (my-web-app)

## Coverage

- Floor: 90% of lines across the whole project.
- Only exclusion: test infrastructure.
- Reference script: `my-rust-tool`'s `scripts/coverage.sh`.

## Docs

- Roadmap: `ROADMAP.md` at the root, shaped like `my-web-app`'s.

## Memories

- Read first: none.
- Update after: none.
