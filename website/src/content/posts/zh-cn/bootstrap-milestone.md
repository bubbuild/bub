---
title: "Baby Bub：从灵感到自举里程碑"
description: "Bub 如何从现代 agent 设计中汲取灵感，以及修复一个 mypy 问题为何是迈向自我改进 AI 的有意义的一步。"
date: 2025-07-16
locale: zh-cn
tags: [milestone, engineering]
---

## 起源：来自现代 Agent 的灵感

Bub 是一个 CLI 优先的 AI agent，秉承"Bub it. Build it."的理念。该项目直接汲取了 [How to Build an Agent](https://ampcode.com/how-to-build-an-agent) 和 [Tiny Agents: Building LLM-Powered Agents from Scratch](https://huggingface.co/blog/tiny-agents) 的灵感。这两份资源提炼了工具使用、循环驱动、可组合、可扩展 agent 的精髓。

但 Bub 也是对自我改进、自托管 agent 新浪潮的回应：想想 Claude Code、SWE-agent，以及更广泛的"自举"运动。目标是：一个不仅能帮你构建，还能帮助构建（和修复）自身的 agent。

## 架构：ReAct 循环、工具与 CLI

### ReAct 循环

Bub 的核心是一个经典的 ReAct 循环，实现在 [`src/bub/agent/core.py`](https://github.com/PsiACE/bub/blob/19c015/src/bub/agent/core.py) 中：

```python
class Agent:
    ...
    def chat(self, message: str, on_step: Optional[Callable[[str, str], None]] = None) -> str:
        self.conversation_history.append(Message(role="user", content=message))
        while True:
            ...
            response = litellm.completion(...)
            assistant_message = str(response.choices[0].message.content)
            self.conversation_history.append(Message(role="assistant", content=assistant_message))
            ...
            tool_calls = self.tool_executor.extract_tool_calls(assistant_message)
            if tool_calls:
                for tool_call in tool_calls:
                    ...
                    result = self.tool_executor.execute_tool(tool_name, **parameters)
                    observation = f"Observation: {result.format_result()}"
                    self.conversation_history.append(Message(role="user", content=observation))
                    ...
                continue
            else:
                return assistant_message
```

这个循环使 agent 能够：

- 解析 LLM 输出中的工具调用（ReAct 模式：思考、行动、行动输入、观察）。
- 执行工具（文件读写/编辑、shell 命令）并将结果反馈到对话中。
- 迭代直到产生"最终答案"。

### 工具系统：可扩展且安全

工具通过 `ToolRegistry`（[`src/bub/agent/tools.py`](https://github.com/psiace/bub/blob/19c015/src/bub/agent/tools.py)）注册，每个工具都是带有验证和元数据的 Pydantic 模型。例如，`RunCommandTool` 会阻止危险命令并验证输入：

```python
class RunCommandTool(Tool):
    ...
    DANGEROUS_COMMANDS: ClassVar[set[str]] = {"rm", "del", ...}
    def _validate_command(self) -> Optional[str]:
        ...
        if base_cmd in self.DANGEROUS_COMMANDS:
            return f"Dangerous command blocked: {base_cmd}"
```

这种设计使得 agent 能够安全地自我修改、运行测试，甚至编辑自己的代码库——这对自我改进至关重要。

### CLI：用户体验与可调试性

CLI（[`src/bub/cli/app.py`](https://github.com/psiace/bub/blob/19c015/src/bub/cli/app.py)）基于 Typer 和 Rich 构建，提供了现代化、用户友好的界面。渲染器（[`src/bub/cli/render.py`](https://github.com/psiace/bub/blob/19c015/src/bub/cli/render.py)）支持调试切换、最小/详细 TAAO（思考/行动/行动输入/观察）输出和清晰的错误报告。

```python
class Renderer:
    def __init__(self) -> None:
        self.console: Console = Console()
        self._show_debug: bool = False
    ...
```

## 里程碑：第一个 mypy 修复（及其意义）

Bub 追求自我改进。第一个切实的里程碑？修复第一个 mypy 错误：为 `Renderer.__init__` 添加缺失的返回类型注解，查看 [commit](https://github.com/PsiACE/bub/commit/87cdcc)。

```diff
-    def __init__(self):
-        self.console = Console()
-        self._show_debug = False
+    def __init__(self) -> None:
+        self.console: Console = Console()
+        self._show_debug: bool = False
```

这个改动将 mypy 错误数量从 24 减少到 23。微不足道？也许吧。但这是一个概念验证：agent 能够推理、定位并修复自身代码库中的类型错误。这是迈向自托管、自修复 agent 循环的第一步——最终它能够：

- 对自身运行静态分析
- 提出并应用代码修复
- 测试和验证改进

## 展望：Bub 作为自举 Agent

Bub 还处于早期阶段。但架构已经就位，支持：

- LLM 驱动的代码编辑和重构
- 自动化类型和 lint 修复
- CLI 驱动的、用户友好的 agent 工作流

从"修复一个 mypy 注解"到"完整的 agent 自我改进"的旅程还很漫长，但每一次自举都始于一个类型安全的步伐。

---

- [GitHub 上的项目](https://github.com/psiace/bub)
- 灵感来源：[ampcode.com/how-to-build-an-agent](https://ampcode.com/how-to-build-an-agent) 和 [huggingface.co/blog/tiny-agents](https://huggingface.co/blog/tiny-agents)
- 另见：Claude Code、SWE-agent，以及更广泛的自举运动
