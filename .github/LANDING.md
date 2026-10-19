# Landing security remediation

`landing-bump.yml` checks open Dependabot alerts weekly (Monday, 02:23 UTC),
on manual dispatch, or when a forwarded alert arrives. It creates draft security
remediation PRs and repairs relevant failed `Main` dependency PRs. Routine upgrades,
general reviews and general issue triage are outside its scope. No dependencies are bumped here.

## Enable

Reuse the existing Landing/Republic configuration: secret `LANDING_API_KEY` and
repository variables `LANDING_MODEL`, `LANDING_API_BASE` and JSON
`LANDING_COMPLETION_ARGS`. No separate model key is needed for Bub. When setting
up a new repository, provision the same authorized configuration through secure
secret management; GitHub cannot reveal existing secret values. Without
`LANDING_MODEL` the job skips.
Set `LANDING_BUMP_ON_FAILURE=false` to disable automatic repair.

The workflow token is the default publishing/admission identity. Its explicit
workflow permissions grant contents, issues, pull requests and Actions write
access plus Dependabot alerts read access. Repository settings allow PR creation.
After creating or repairing a candidate, Landing explicitly dispatches `Main`
on its branch, so native checks run even when the workflow token authored the push.

Optional `LANDING_GITHUB_TOKEN` overrides publication; `LANDING_ADMISSION_TOKEN`
separately overrides admission. Preserve these existing conventions when using
an organization-owned App or authorized machine account. Set `LANDING_GIT_NAME`
and `LANDING_GIT_EMAIL` for that account's commit identity.

Repair admission accepts Dependabot, Renovate and the prepared publisher on
`deps/landing-*` branches. Optional `LANDING_DEPENDENCY_UPDATE_ACTORS` adds allowed
logins, one per line. Only failed `Main` PR runs or dispatches on non-default
candidate branches qualify. Landing validates an unambiguous open same-repository
PR matching the failed run; the task must establish its security purpose before
editing. The runner initially checks out the trusted default branch.

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
one. Open alerts route to bump. Fixed/dismissed alerts intentionally route through
Landing triage to reconcile existing tracked remediation, even though the workflow
requests `command: bump`. This narrow reconciliation authorizes no upgrade or
general triage; dismissal alone does not prove repair. Unavailable alert access
leaves coverage unknown. Listeners must reach the default branch before receiving these
events. See [GitHub events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).

## Validate candidates

Use `uv` and `pnpm` for scoped manifest/lock changes, preserving constraints and
Bub's hatch-vcs version policy. Run `make check`, `make test` and affected website
checks, then verify independent `Main` results for the candidate head (Python
3.12/3.13/3.14 and docs). After each candidate push, dispatch the unchanged native
jobs on the candidate branch:

```bash
gh workflow run main.yml --ref <candidate-branch>
```

Verify the run's `head_sha` matches the published candidate head; a superseded
head needs fresh checks. Successful runs require no Landing follow-up. Failed
candidate dispatches can enter the same scoped repair path as failed PR runs.
New candidates remain drafts; repairs retain the PR's
status and branch. Maintainers control merges, releases and dismissals.
Landing evidence is retained as a workflow artifact for 14 days.
