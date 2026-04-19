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
- Environment variable: `NODE_VERSION=22.16.0`
- Build secret: `GITHUB_TOKEN=<GitHub PAT>` (optional, recommended for higher GitHub API limits)

The repo keeps a minimal [wrangler.jsonc](./wrangler.jsonc) and relies on
Astro/Wrangler's default Cloudflare integration for the generated Worker
configuration.

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

- `just docs`
- `just docs-test`
- `just docs-preview`

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
