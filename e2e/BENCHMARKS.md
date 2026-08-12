# Benchmark coverage

This reference describes the external tasks used to evaluate Bub as an agent. The harness does not copy or rewrite benchmark tasks. Harbor resolves each declared dataset and task, runs its native verifier, and records the resolved task checksum. Bub and its plugins are installed through their public commands for every case.

## Selection policy

A benchmark case belongs in this repository when it satisfies all of these conditions:

- it measures observable agent behavior rather than a model-only trivia score;
- it provides an executable environment and a native outcome verifier through Harbor;
- its dataset revision, task ID, and task checksum can be recorded in the case manifest;
- its workload exercises a Bub capability that is not already represented by an equivalent cheaper case; and
- its evaluator limitations are explicit.

The suite uses two cases per capability family: one relatively small case and one case that adds parallelism, scale, or a longer execution path. `smoke` contains only one deterministic representative per major family. It is intended to detect integration breakage, not to reproduce a benchmark leaderboard.

## Cases

| Capability family | Case | Upstream task | What the native verifier observes | Tier |
| --- | --- | --- | --- | --- |
| Structured action selection | `benchmark-bfcl-simple` | `bfcl_parity@1.0 / bfcl-simple-python-325` | Exact selected function and arguments in a JSON artifact | `smoke` |
| Structured action selection | `benchmark-bfcl-parallel` | `bfcl_parity@1.0 / bfcl-parallel-multiple-130` | Ordered set of three applicable calls and typed arguments | full |
| Terminal and system work | `benchmark-tbench-regex` | `terminal-bench-sample@2.0 / regex-log` | Regex behavior against positive and adversarial log lines | `smoke` |
| Terminal and system work | `benchmark-tbench-git-webserver` | `terminal-bench-sample@2.0 / configure-git-webserver` | End-to-end Git push hook and HTTP-served artifact | full |
| Spreadsheet artifacts | `benchmark-spreadsheet-formula` | `spreadsheetbench-verified@1.0 / 46240` | Formula values and preservation of the workbook outside the target range | `smoke` |
| Spreadsheet artifacts | `benchmark-spreadsheet-scoring` | `spreadsheetbench-verified@1.0 / 42526` | Formulas, number formats, fill color, and workbook preservation | full |
| General assistant and cross-file reasoning | `benchmark-gaia-archive` | `gaia@1.0 / 9b54f9d9-35ee-4a14-b62f-d130ea00317f` | Exact answer derived from spreadsheet and XML files in an archive | `smoke` |
| General assistant and multimodality | `benchmark-gaia-chess` | `gaia@1.0 / cca530fc-4052-43b2-b130-b30968d8aa44` | Exact winning chess move derived from an image | full |
| Build and long-horizon execution | `benchmark-compile-cowsay` | `compilebench@1.0 / cowsay` | Installed executable, help behavior, output, and packaged assets | `smoke` |
| Build and long-horizon execution | `benchmark-compile-coreutils` | `compilebench@1.0 / coreutils-static` | Static linkage, installed utilities, version behavior, and SHA-1 output | full |
| Repository research | `benchmark-swe-atlas-qna` | `scale-ai/swe-atlas-qna@1.0 / scale-ai/task-6905333b74f22949d97ba9c8` | Programmatic checks plus an expert rubric over a runtime-grounded security analysis | `judge` |
| Test engineering | `benchmark-swe-atlas-tests` | `scale-ai/swe-atlas-tw@1.0 / scale-ai/task-6902ef3ab97fe23e2ad27207` | Test execution, manifest checks, and an expert rubric over added integration tests | `judge` |

The capability labels describe the selected task, not every capability claimed by the upstream benchmark. For example, the Harbor BFCL parity adapter asks a general ACP agent to emit the selected calls as JSON; it does not inject the declared functions into Bub's native tool registry. The case therefore measures action selection and structured output, while Tape separately proves that Bub used its own tools to create the artifact.

## Why these sources

- [BFCL](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard) is an executable Berkeley benchmark covering simple, multiple, parallel, and irrelevant function-call decisions.
- [Terminal-Bench 2.0](https://openreview.net/forum?id=a7Qa4CcHak) is an ICLR 2026 benchmark of 89 curated terminal tasks with isolated environments and executable tests. The harness uses its official ten-task sample dataset.
- [SpreadsheetBench](https://papers.nips.cc/paper_files/paper/2024/hash/ac840df270ac537dd74530a15c332684-Abstract-Datasets_and_Benchmarks_Track.html) was published in the NeurIPS 2024 Datasets and Benchmarks track and derives tasks from real spreadsheet questions. The selected Harbor dataset is the expert-annotated 400-task Verified subset.
- [GAIA](https://openreview.net/forum?id=fibxvahvs3) was published at ICLR 2024 and targets reasoning, tool use, file handling, browsing, and multimodality. The selected tasks are self-contained and do not require live web search.
- [CompileBench](https://www.compilebench.com/about/) evaluates end-to-end builds of real open-source projects and verifies the resulting binaries rather than source-code style.
- [SWE Atlas](https://labs.scale.com/papers/sweatlas) covers codebase question answering, test writing, and refactoring with runtime exploration and category-specific evaluation. Its evaluator-model dependency is why these cases have a separate tier.

Harbor itself owns task adaptation and parity validation. Its [adapter requirements](https://www.harborframework.com/docs/datasets/adapters) require stable task identifiers, passing oracle solutions, and parity checks before publication. Bub's harness adds an independent checksum assertion at run time.

## Deliberate exclusions

- SWE-bench Verified is not a core signal. [OpenAI's 2026 audit](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/) reports contamination and material task-design problems and states that it no longer measures frontier coding capability reliably. Historical popularity does not outweigh a current upstream warning.
- DABstep is not in the pinned suite because its current Harbor task Dockerfiles fetch context files from an unversioned `main` URL during image construction. The task directory checksum therefore does not fully pin the evaluated input.
- Benchmarks that require a harness-specific user simulator, browser service, or proprietary environment are not adapted locally. They can be added when Harbor provides a stable upstream dataset so this repository does not become a second benchmark framework.

## Run and interpret the suites

Run the deterministic smoke set:

```console
BUB_E2E_CATEGORIES=smoke make e2e-run
```

Run every selected benchmark case, including evaluator-model cases:

```console
BUB_E2E_CATEGORIES=benchmark make e2e-run
```

Run only evaluator-model cases:

```console
BUB_E2E_CATEGORIES=judge make e2e-run
```

`summary.json` contains suite pass rate and operational totals. A case passes only when the task checksum matches, Harbor's native verifier reaches the declared reward, required ACP and Tape artifacts exist, Bub completes, and budgets hold. Tape-derived steps, model calls, tool calls, tool errors, tokens, anchors, and handoffs remain separate metrics; they are evidence for diagnosing or comparing runs, not a synthetic quality score.

Judge cases require the evaluator credentials expected by their upstream task. They should be compared only when the evaluator model and endpoint are held constant. Missing provider token usage is reported as unavailable rather than zero.
