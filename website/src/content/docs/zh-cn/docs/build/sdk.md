---
title: 构建自己的 Agent
description: 选择模型、添加工具，把 Bub Agent 接入自己的 Python 程序。
sidebar:
  order: 0
---

选择模型，加入工具，把 Agent 接到你的程序里。`bub.core.Agent` 负责模型与工具之间的循环；你决定如何保存会话，以及它怎样与你的系统协作。

Agent 的组件由你直接提供。[Bub 应用](/zh-cn/docs/build/)建立在同一个内核上，提供配置和插件发现能力。你也可以在自有服务中[复用应用插件](/zh-cn/docs/build/reuse-plugins/)，保留现有入口和调用代码。

## 安装

使用 Python 3.12+，将 Bub 加入项目：

```bash
uv init my-bub
cd my-bub
uv add bub
```

基础 `bub` 包安装 Agent 所需的依赖。需要 Bub 应用的 CLI、插件或渠道时，再使用 `bub[app]`。其他环境设置方式见[安装指南](/zh-cn/docs/getting-started/install/)。

<a id="create-an-agent"></a>

## 运行第一个 Agent

在项目中保存为 `sdk_example.py`：

```python
import asyncio
from pathlib import Path

import republic

from bub.core import Agent
from bub.core.store import FileTapeStore
from bub.core.tools import Tool


async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up the status of an order."""
    return {"order_id": order_id, "status": "shipped"}


async def main() -> None:
    agent = Agent(
        store=FileTapeStore(Path(__file__).resolve().parent / "sessions"),
        tools=[Tool.from_callable(lookup_order)],
        system_prompt="Use lookup_order to check order status before answering.",
    )
    async with republic.get_model("openai:gpt-5-mini") as model:
        async with agent.run(
            "Where is order A123?",
            model=model,
            options={"max_tokens": 4096},
            tape="customer-42",
        ) as stream:
            async for event in stream:
                if event.kind == "text":
                    print(event.data.get("delta", ""), end="", flush=True)
                elif event.kind == "error":
                    raise RuntimeError(str(event.data.get("message", "Agent failed")))
        print()


if __name__ == "__main__":
    asyncio.run(main())
```

在 shell 中设置 `REPUBLIC_OPENAI_API_KEY`，然后运行：

```bash
uv run python sdk_example.py
```

工具返回示例订单状态。接入实际应用时，把函数体换成自己的订单服务。Agent 将工具结果交回模型，由模型生成回答。

使用相同的 `tape` 名称再次运行程序，就会继续 `sessions/` 中保存的会话。换一个名称即可开始独立会话。

<a id="streams-and-lifecycle"></a>

## 管理运行与输出流

`agent.run()` 返回一个流，迭代这个流会推进执行：调用模型、执行模型请求的工具，再次调用模型，直到模型回答且不再请求工具。一个 `final` 事件表示一个模型步骤结束；完整消费流，才能完成这一轮运行。

用 `async with` 管理流。无论正常结束、抛出异常，还是提前 `break`，退出代码块都会关闭流、释放打开的模型流，并合并本轮会话记录。运行失败也会保留已经写入的记录，详见[分支与部分结果](/zh-cn/docs/concepts/tape-and-context/#forks)。

如果另一个组件负责消费输出，可以用参数相同的 `await agent.start(...)` 取得流。接收它的组件必须负责关闭，包括尚未开始迭代的情况。自己消费输出时，优先使用 `run()`。

流事件包括 `text`、`reasoning`、`tool_call`、`tool_result`、`usage`、`error` 和 `final`。需要同时处理错误事件和迭代时抛出的异常。观察内部生命周期或拦截调用，请使用 [Agent 事件](/zh-cn/docs/concepts/events/)。

<a id="models"></a>

## 选择模型

将 Republic chat model 传给 `Agent(model=...)`，可以设置默认模型；传给 `agent.run(model=...)`，则只用于当前运行。如果运行需要模型，而两处都没有提供，就会抛出 `ValueError`。模型的生命周期由你管理，请用 `async with` 包住使用它的运行过程。

Republic 可以直接接收凭据和服务地址：

```python
async with republic.get_model(
    "openai:gpt-5-mini", api_key="...", api_base="https://example.test/v1"
) as model:
    agent = Agent(model=model)
    async with agent.run("Hello", tape="example", options={"max_tokens": 4096}) as stream:
        async for event in stream:
            print(event.kind, event.data)
```

`options` 是每次模型调用使用的 Republic chat options，Agent 会补入当前步骤的工具。这个客户端通过 Republic 参数或环境变量配置；`BUB_*` 设置用于 Bub 应用。

如果需要自己的模型回退、凭据选择或超时策略，可以实现 `bub.core.ModelClient`：

| 成员 | 要求 |
| --- | --- |
| `spec` | 默认的 `provider:model` 字符串。 |
| `max_tokens` | 默认输出 token 上限，或 `None`。 |
| `open_stream(messages, *, spec, options)` | 异步上下文管理器，产出 `(provider_name, republic_stream)`。messages 是 `republic.Message` 对象，options 包含本步骤的工具。 |

`LLMCallStarted` 处理器可以更改请求的模型。使用 Republic chat model 时，同一供应商内的切换复用当前客户端；切换到其他供应商会读取 Republic 环境配置创建客户端。需要不同策略时，提供自己的 `ModelClient`。

<a id="system-prompt"></a>

## 设置指令

`system_prompt` 接收文本、函数，或按顺序排列的两者。函数接收 `PromptContext`，支持同步和异步。Agent 在每个模型步骤前渲染这些片段，忽略空结果，再用空行拼接。

```python
from bub.core import PromptContext


@agent.system_prompt.add
def customer(context: PromptContext) -> str | None:
    if (name := context.state.get("user")) is not None:
        return f"You are helping {name}."
    return None
```

`PromptContext` 包含当前步骤的 prompt、提供给模型的工具、模型标识、Tape、本轮状态和 Agent。`available_tools` 列出 Agent 注册的工具。使用 `agent.system_prompt.remove(section)` 可以移除片段。

<a id="tapes-and-state"></a>
<a id="sessions-and-tapes"></a>

传给运行的 `state` 是可变字典，由工具、提示词片段和事件处理器共用。哪些值需要保存、下次运行时如何恢复，由你决定，详见[本轮状态](/zh-cn/docs/concepts/tape-and-context/#state)。

<a id="tools"></a>

<a id="agent-parameters"></a>

## Agent 参数

| 参数 | 默认值与用途 |
| --- | --- |
| `model` | `None`。默认 Republic chat model 或 `ModelClient`。 |
| `store` | 新建的 `InMemoryTapeStore`。也可以传入同步或异步存储。 |
| `tools` | 空。接收 `Tool` 对象，以及 `republic.tools.WebSearch()` 等供应商执行的工具。 |
| `system_prompt` | 空。每个步骤渲染的文本或函数片段。 |
| `context` | `default_tape_context()`。选择用于模型消息的会话记录。 |
| `sidecars` | 空。与主 Tape 一起管理的附加存储能力。 |
| `max_steps` | `sys.maxsize`。一轮运行的模型步骤上限。 |
| `max_tool_concurrency` | `None`，同一次模型响应中的工具调用可以全部并发。设为 `1` 时依次执行。不同运行之间的并发由应用协调。 |
| `name` | `"agent"`。Agent trace span 使用的名称。 |
| `command_prefix` | `None`。设置前缀后，可以通过文本命令直接调用工具。 |

传入的 `Tool` 按名称保存在 `agent.tools` 中，供应商工具保存在 `agent.builtin_tools` 中。构造或更新 Agent 时，由你显式选择这些工具。

<a id="customize-the-loop"></a>

## 定制步骤准备方式

当工具选择需要自己的业务逻辑时，可以继承 `Agent` 并重写 `prepare_tools(tape, prompt)`。默认提供传入的工具，排除 `exposure="command"`。`tape.context.state` 就是本轮状态。

运行过程中到达的新消息和继续运行时的下一步提示，由两个循环事件控制：

| 事件 | 发生时机 | 支持的操作 |
| --- | --- | --- |
| `MessagesPending` | 每个步骤构建模型请求时，以及步骤结束且没有调用工具时。 | 向 `event.messages` 追加内容；它们在下一个 prompt 之前发送，并一起记录。步骤结束时提供的消息会让运行继续。 |
| `StepFinished` | 步骤结束。`status == "continue"` 时运行会再走一步。 | 设置 `next_prompt` 为下一步添加用户消息；为 `None` 时直接继续对话。`event.prompt` 是本步骤的 prompt，`event.stream` 保存本次运行的用量。 |

```python
from bub.core.events import MessagesPending, StepFinished

inbox: dict[str, list[str]] = {}


@agent.events.on(MessagesPending)
def send_queued(event: MessagesPending) -> None:
    event.messages.extend([text] for text in inbox.pop(event.tape.name, []))


@agent.events.on(StepFinished)
def check_results(event: StepFinished) -> None:
    if event.status == "continue":
        event.next_prompt = ["Check the tool results before answering."]
```

如果要自己管理 Tape 生命周期，可以打开 `agent.session(name, state=...)`，再调用 `agent.run_on(tape, prompt, model=...)`。它返回的流需要由你关闭。`agent.run_once(prompt, model=..., tape=...)` 只产出一个模型步骤，适合自己驱动循环的场景。

<a id="events"></a>
<a id="commands"></a>

## 继续构建

- [添加工具与 CodeMode](/zh-cn/docs/build/tools/)，让 Agent 执行动作。
- [保存会话历史](/zh-cn/docs/concepts/tape-and-context/)，在多轮运行之间继续工作。
- [通过 FastAPI 提供服务](/zh-cn/docs/tutorials/fastapi/)，让其他程序通过 HTTP 调用 Agent。
