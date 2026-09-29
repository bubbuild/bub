# Website Deployment

## Goal

The new `website/` directory is the docs and marketing site for Bub.

Legacy MkDocs source files may still exist in the repository during the
transition, but production deployment now targets the Astro site on
Cloudflare Workers.

## Cloudflare Workers

Connect the repository to a Cloudflare Worker using Git integration.

Recommended settings:

- Project name: `bub`
- Build command: `pnpm install --frozen-lockfile && pnpm build`
- Deploy command: `pnpm wrangler deploy`
- Path: `website`
- Environment variable: `SITE_URL=https://bub.build`
- Node version: `24.21.0` (pinned in both `.node-version` files; remove any older
  `NODE_VERSION` override, or set it to `24.21.0`)
- Build secret: `GITHUB_TOKEN=<GitHub PAT>` (optional, recommended for higher GitHub API limits)

The repo keeps a minimal [wrangler.jsonc](./wrangler.jsonc) and relies on
Astro/Wrangler's default Cloudflare integration for the generated Worker
configuration.

Keep the repository and website Node pins aligned so Cloudflare and GitHub
Actions install the same dependencies. Node 22.12 causes pnpm to skip
`@napi-rs/wasm-runtime`, which requires Node 22.13+ on the 22.x line; the
Cloudflare bundle then fails to resolve it. Other dependencies require Node
22.19+, so the website enforces that minimum during installation. After changing
the build Node version, clear Cloudflare's build cache and retry if a cached
installation still lacks the WASM runtime.

Astro sessions are explicitly disabled because the site does not use per-user
server state. The generated Worker should have only the `ASSETS` binding, with
no automatically provisioned `SESSION` KV namespace. Verify this with
`pnpm wrangler versions upload --dry-run` after building.

GitHub repo stats are snapshotted during `pnpm build` into
`src/data/github-snapshot.ts`. The Worker does not call the GitHub API at
runtime, so `GITHUB_TOKEN` only needs to exist as a build secret.

The repo also includes [public/.assetsignore](./public/.assetsignore) for the
SSR Worker build:

- `_worker.js`
- `_routes.json`

Production deployment is handled by Cloudflare Workers Git integration instead of
GitHub Actions.

## Current Repo State

The local developer entrypoints now target the new site:

- `make docs`
- `make docs-test`
- `make docs-preview`

The CI docs check also builds `website/` instead of MkDocs.

## GitHub Actions and Cloudflare Responsibilities

The deployment split is intentionally simple:

- `main.yml` only verifies that the website builds
- `on-release-main.yml` only handles package release tasks
- Cloudflare Workers deploys the website from the connected repository

Required Cloudflare Workers project configuration:

- Git integration enabled for this repository
- Build command set to `pnpm install --frozen-lockfile && pnpm build`
- Deploy command set to `pnpm wrangler deploy`
- Working directory set to `website`
