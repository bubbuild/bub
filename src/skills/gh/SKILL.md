---
name: gh
description: GitHub CLI skill for interacting with GitHub via the gh command line tool. Use when Bub needs to (1) Create, view, or manage GitHub repositories, (2) Work with issues and pull requests, (3) Create and manage releases, (4) Run and monitor GitHub Actions workflows, (5) Create and manage gists, (6) Install and manage agent skills, or (7) Perform any GitHub operations via command line.
---

# GitHub CLI (gh) Skill

Interact with GitHub using the gh command line tool.

Run commands with Bub's `bash` tool, setting `cwd` to the target checkout when using local repository context. If it returns a running shell ID, collect the result with `bash.output` before retrying the command.

## Prerequisites

Use the authentication source specified by the user or workspace. For github.com, `GH_TOKEN` takes precedence over `GITHUB_TOKEN`, and both override stored `gh` credentials.

Check authentication:
```bash
gh auth status
```

If not authenticated, report the blocker; do not silently switch credentials. Run interactive login only when the user requests authentication setup:
```bash
gh auth login
```

## Repository Operations

```bash
gh repo create <name> [--public|--private]
gh repo clone <owner/repo>
gh repo fork <owner/repo>
gh repo view [owner/repo]
gh repo list [owner]
```

## Issue Operations

```bash
gh issue create --title "Title" --body "Body"
gh issue list [--state open|closed]
gh issue view <number>
gh issue close <number>
gh issue comment <number> --body "Comment"
```

## Pull Request Operations

```bash
gh pr create --title "Title" --body "Body"
gh pr list [--state open|closed]
gh pr view <number>
gh pr checkout <number>
gh pr merge <number>
gh pr review <number> --approve
```

## Release Operations

```bash
gh release create <tag> --generate-notes
gh release list
gh release download <tag>
gh release upload <tag> <file>
```

## Workflow Operations

```bash
gh workflow list
gh workflow run <name>
gh run list
gh run view <run-id>
gh run watch <run-id>
```

## Gist Operations

```bash
gh gist create <file>
gh gist list
gh gist view <id>
```

## Skill Operations

```bash
gh skill search <query>
gh skill preview <owner/repo> <skill>
gh skill install <owner/repo> <skill> --dir .agents/skills
gh skill list --dir .agents/skills
gh skill update <skill> --dir .agents/skills --dry-run
gh skill update <skill> --dir .agents/skills --all
```

`gh skill` is in preview; check `gh skill --help` for availability. Run project-scoped commands from Bub's workspace and use `--dir .agents/skills`; use `--dir "$HOME/.agents/skills"` for user-scoped skills. Keep gh's source-tracking metadata so installed skills can be updated.

## Tips

- Use --web to open in browser
- Use -R owner/repo to specify repository
- Use --json with --jq for scripting
- Use `GH_PROMPT_DISABLED=1` and supply required flags for unattended execution.
- Write issue, PR, and comment body files as UTF-8 Markdown with real newlines. Pass them with `--body-file`, or use `--body-file -` with a quoted heredoc to preserve shell-sensitive text.
- Use an explicit `--head` with `gh pr create` to avoid implicit pushes or forks; the head branch must already exist remotely.
