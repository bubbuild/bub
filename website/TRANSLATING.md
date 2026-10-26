# Translating the docs

Applies to every locale under `src/content/docs/`. The English source is `src/content/docs/docs/`. Keep its paragraph and heading structure; do not merge, split, or reorder.

## Glossary

A term that names code keeps its spelling. Do not invent a local name. A new locale adds a column.

Anything not listed is an ordinary technical word. Translate it the usual way; judge each case. Keep one rendering per page.

| Term | zh-cn | Note |
| --- | --- | --- |
| `turn` | `turn` | One inbound message and all its work. Never 轮次, 回合. |
| `tape` | `tape` | The conversation record. Never 磁带. |
| `anchor` | `anchor` | Where the context starts. |
| `handoff` | `handoff` | Writes a new anchor. |
| `state` | `state` | The dictionary a turn shares. Type `TurnState`. |
| `steering` | `steering` | Never 引导. |
| `queue up` | `queue up` | Never 排队. |
| `hook` | `hook` | Never 钩子. |
| `envelope` | `envelope` | Never 信封. |
| `trace` | `trace` | Never 轨迹, 追踪. |
| `CodeMode` | `CodeMode` | The class. The feature is `code mode`. Never 代码模式. |
| `extra` | `extra` | User-facing prose says "`app` extra" and "`trace` extra"; keep `extra` for the packaging mechanism, `--extra`, and `package[extra]`. |
| `harness` | `harness` | The app layer around the loop. |

Docs pages explain the `steering` and `queue up` difference where it changes the reader's choice. A steering message that the current turn does not consume falls back to the pending queue; pages that mention steering keep that fallback rule. For the general statement, link [Steering and queueing](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/steering-and-queueing) instead of translating it.

## Punctuation (zh-cn)

- Use full-width `，。；：！？、` and `「」`.
- Put one space between Chinese and Latin letters, digits, or inline code.
- Use `……` for an ellipsis and `——` for a dash.
- Put no space next to full-width punctuation, and none inside bold or italic text.
