# Landing security remediation

`landing-bump.yml` checks open Dependabot alerts weekly (Monday, 02:23 UTC),
on manual dispatch, or when a forwarded alert arrives. It creates draft security
remediation PRs and repairs relevant failed `Main` dependency PRs. Routine upgrades,
general reviews and triage are outside its scope. No dependencies are bumped here.

## Enable

Reuse the existing Landing/Republic configuration: secret `LANDING_API_KEY` and
repository variables `LANDING_MODEL`, `LANDING_API_BASE` and JSON
`LANDING_COMPLETION_ARGS`. No separate model key is needed for Bub. When setting
up a new repository, provision the same authorized configuration through secure
secret management; GitHub cannot reveal existing secret values. Without
`LANDING_MODEL` the job skips.
Set `LANDING_BUMP_ON_FAILURE=false` to disable automatic repair.

Prepare secret `LANDING_GITHUB_TOKEN` with repository contents, pull requests,
issues and Actions write access and Dependabot alerts read access. Use an
organization-owned App or authorized machine account whose writes trigger
independent CI. Set `LANDING_GIT_NAME` and `LANDING_GIT_EMAIL` for its commit
identity. Optional `LANDING_ADMISSION_TOKEN` separates admission from publication.
The fallback workflow token needs repository permission to create PRs and cannot
trigger `Main` from its own writes. `Main` has no manual dispatch; missing
independent CI is a blocker, not permission to change native CI or claim success.

Repair admission accepts Dependabot, Renovate and the prepared publisher on
`deps/landing-*` branches. Landing validates an unambiguous open same-repository
PR matching the failed run; the task must also establish its security purpose
before editing. The runner initially checks out the trusted default branch.

The public Action is pinned to Landing revision
`9014baa64f82893dc194a3f1e31391737f68c426`; Landing's self-hosted `uses: ./`
example is not used here. See the pinned [Action API](https://github.com/bubbuild/landing/blob/9014baa64f82893dc194a3f1e31391737f68c426/action.yml)
and [GitHub guide](https://github.com/bubbuild/landing/blob/9014baa64f82893dc194a3f1e31391737f68c426/docs/guides/github.md).

## Forward alerts

Dependabot alerts are webhooks, not native Actions triggers. A separately deployed
receiver must verify GitHub's webhook signature and dispatch to this repository:

```json
{
  "event_type": "dependabot_alert",
  "client_payload": {"alert_number": 7}
}
```

No receiver or subscription is installed here. Scheduled/manual scans work without
one. Fixed/dismissed alerts authorize no upgrades; unavailable alert access leaves
coverage unknown. Listeners must reach the default branch before receiving these
events. See [GitHub events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).

## Validate candidates

Use `uv` and `pnpm` for scoped manifest/lock changes, preserving constraints and
Bub's hatch-vcs version policy. Run `make check`, `make test` and affected website
checks, then verify independent `Main` results for the candidate head (Python
3.12/3.13/3.14 and docs). New candidates remain drafts; repairs retain the PR's
status and branch. Maintainers control merges, releases and dismissals.
Landing evidence is retained as a workflow artifact for 14 days.
