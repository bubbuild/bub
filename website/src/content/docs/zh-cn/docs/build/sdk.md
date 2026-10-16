---
title: Python SDK
description: 在 Python 应用中嵌入 Bub 的 agent loop，使用你自己的模型、工具、提示词和会话存储。
sidebar:
  order: 0
---

`bub.core` 是 Bub 的 agent 内核。它的 `Agent` 运行 agent loop（调用模型、执行模型请求的工具、重复直到模型给出回答），
所用的组件都由你提供：工具、system prompt section 和 tape store，以及传给 agent 或每次运行的 Republic 模型。
它不读取 Bub 的 settings，也不加载插件，因此可以嵌入任何应用。

Bub 自身的运行时也构建在这个内核之上：`bub.builtin.BubAgent` 在其上加入 settings、插件钩子、skills、命令和 steering。

## 安装

在 Python 3.12+ 项目中安装 Bub：

```bash
uv add bub
```

这只会安装内核。如果还需要 `bub` 命令、channel 或插件，请安装 `app` extra（`bub[app]`）。

下文描述的是当前源码中的 API；请使用包含这些接口的发布版本，或安装当前源码。

## 创建 Agent

将以下内容保存为 `sdk_example.py`：

```python
import asyncio
from pathlib import Path

import republic

from bub.core import Agent
from bub.core.store import FileTapeStore
from bub.core.tools import Tool


async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up the status of an order."""
    # 将这个演示返回值替换为你应用中的订单服务。
    return {"order_id": order_id, "status": "shipped"}


def create_agent() -> Agent:
    root = Path(__file__).resolve().parent
    return Agent(
        store=FileTapeStore(root / "sessions"),
        tools=[Tool.from_callable(lookup_order)],
        system_prompt=[
            "You are an order assistant. Reply directly to the user.",
            "Use lookup_order to check order status before answering.",
        ],
    )


async def main() -> None:
    agent = create_agent()
    # 任意 Republic "provider:model"。API key 从 REPUBLIC_OPENAI_API_KEY 读取。
    async with republic.get_model("openai:gpt-5-mini") as model:
        async with agent.run(
            "Where is order A123?", model=model, options={"max_tokens": 4096}, tape="customer-42"
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

用 `REPUBLIC_OPENAI_API_KEY=... uv run python sdk_example.py` 运行。使用同一个 `tape` 再次运行，会继续保存在 `sessions/` 下的对话。

| `Agent(...)` 参数 | 行为                                                                              |
| ----------------- | --------------------------------------------------------------------------------- |
| `model`           | 未传入模型的运行所用的默认模型；见 [模型](#模型)。默认 `None`，即每次运行都需传入。 |
| `store`           | `TapeStore` 或 `AsyncTapeStore`。默认 `InMemoryTapeStore()`。                     |
| `tools`           | 模型可调用的 `Tool` 对象，按名字保存在 `agent.tools` 中；也可传入由 provider 自行执行的 Republic 内置工具（如 `republic.tools.WebSearch()`），保存在 `agent.builtin_tools` 中，随每次模型调用发送。 |
| `system_prompt`   | 一个或一组 system prompt section；见 [System prompt](#system-prompt)。            |
| `context`         | 决定哪些 tape 条目进入模型上下文的 `TapeContext`。默认 `default_tape_context()`。 |
| `sidecars`        | 挂载到每条 tape 上的 `TapeSidecar` 能力。                                         |
| `max_steps`       | 一次运行中最多的模型调用次数。                                                    |
| `name`            | 在 `invoke_agent` trace 中上报的 agent 名称。默认 `"agent"`。                     |
| `command_prefix`  | 标记命令 prompt 的前缀，例如 `","`；见 [命令](#命令)。默认 `None`，即不启用命令。 |

## 模型

每次运行调用传入的模型，未传入时调用 agent 的 `model`，因此同一个 agent 可以用不同模型服务不同的运行；两者都没有时，
需要调用模型的运行会抛出 `ValueError`。传入 `republic.get_model()` 得到的 Republic
chat model 即可；它的生命周期归你管理，请在使用它的运行外层用 `async with` 打开（或自行关闭）。凭据和端点属于 Republic
的客户端参数：

```python
model = republic.get_model("openai:gpt-5-mini", api_key="...", api_base="https://example.test/v1")
async with agent.run(prompt, model=model, options={"max_tokens": 4096, "temperature": 0.2}, tape="t") as stream:
    ...

agent = Agent(model=model)  # 未传入模型的运行调用它
async with agent.run(prompt, tape="t") as stream:
    ...
```

`options` 是随本次运行每次模型调用发送的 `republic.ChatOptions`；循环会加入本步的工具。`LLMCallStarted`
处理器可以把一次调用重定向到其他 `provider:model`：同一 provider 的模型复用运行模型的客户端，其他 provider
使用从 Republic 环境变量配置的客户端。

如果需要自己的策略，例如 fallback、按 provider 的凭据或整次调用的超时，可以改为传入实现 `bub.core.ModelClient`
的对象：提供 `spec`（默认的 `provider:model`）、默认 `max_tokens`（或 `None`），以及
`open_stream(messages, spec=..., options=...)`——一个产出 provider 名称和 `republic.Stream` 的异步上下文管理器。
Bub 自己的 `bub.builtin.model.BubModel` 就是这样的客户端。

## 工具

`Tool.from_callable()` 从类型注解生成参数 schema，从 docstring 生成描述，不会注册到全局。`bub.core.tools` 中的
`@tool` 装饰器生成同样的 `Tool`，同时把它加入 Bub 的全局注册表，Bub 自己的 agent 会取其快照。

以 `context=True` 创建的工具会收到 `ToolContext`，其中包含本次运行的 `tape`、turn `state`、`run_id` 和事件总线：

```python
from bub.core.tools import ToolContext


def remember(note: str, *, context: ToolContext) -> str:
    """Remember a note for this conversation."""
    context.state.setdefault("notes", []).append(note)
    return "noted"


agent.tools["remember"] = Tool.from_callable(remember, context=True)
```

工具出错时，错误会作为工具结果返回给模型，而不会结束运行。名字中带 `.` 的工具会以 `_` 替换后提供给模型
（例如 `fs.read` 变为 `fs_read`）。

## System prompt

system prompt 是有序的 section 列表，每个模型步骤渲染一次，以空行连接。section 可以是文本，也可以是接收
`PromptContext` 并返回文本的同步或异步函数；返回空串或 `None` 时跳过该 section。`agent.system_prompt.add(section)`
追加一个 section 并返回它，因此也可以用作装饰器；`agent.system_prompt.remove(section)` 将其移除。

```python
from bub.core import PromptContext


@agent.system_prompt.add
def tool_list(context: PromptContext) -> str:
    names = ", ".join(tool.name for tool in context.tools)
    return f"Tools available in this step: {names}."


@agent.system_prompt.add
async def customer(context: PromptContext) -> str | None:
    if (user := context.state.get("user")) is None:
        return None
    return f"You are talking to {user}."
```

context 包含本步的 `prompt`（工具调用后继续时为空）、本步模型可见的 `tools`、本步 `model` 的 `provider:model`、
本次运行的 `tape` 与 `state`，以及 `agent`（并提供 `available_tools` 快捷属性）。

## 会话与 tape

每次运行使用 agent store 中以 `tape=` 命名的 tape。复用同一个名字即可继续对话；新对话使用新名字。运行写入的是该 tape 的
fork，运行的流关闭时合并回去；传入 `merge_back=False` 则丢弃。

`state=` 是本次运行可修改的 turn state：一个由 system prompt section、工具和事件处理器共享的字典。agent 不会持久化它；
需要保留的内容请放在你的应用中或写到 tape 上。

```python
async with agent.run("Any news on A123?", model=model, tape="customer-42", state={"user": "Ada"}) as stream:
    ...

tape = agent.tape("customer-42")
messages = await tape.read_messages()  # 以 Republic 消息表示的对话。
await tape.handoff(name="summary", state={"summary": "Ada asked about order A123."})
```

`agent.tape(name)` 返回某个对话的 tape，供运行之外使用；它直接写入 store。默认情况下模型上下文是该 tape
最近一个 anchor 之后记录的对话，因此 `handoff()` 会开始一个更短的上下文。如果某次调用因上下文过长而失败，运行会自动
handoff 一次并重试该 prompt。

## 命令

设置了 `command_prefix` 后，以该前缀开头的文本 prompt 会作为命令执行，而不走 agent loop，也不会调用模型。`name args...`
调用名为 `name` 的工具，先传位置参数（按顺序对应工具的参数），再传 `key=value` 参数；其他命令在 agent 有 `bash` 工具时交给它执行。输出作为
本次运行的 text 和 `final` 事件返回；同时发出 `CommandFinished` 事件报告这次命令，tape 会将其记录为 `command` 事件。

```python
agent = Agent(tools=[Tool.from_callable(lookup_order)], command_prefix=",")

async with agent.run(",lookup_order order_id=A123", model=model, tape="customer-42") as stream:
    async for event in stream:
        print(event.kind, event.data)  # 依次是 text 和 final，内容为工具渲染后的结果。
```

首尾空白会被忽略，且只去掉一个前缀；只有前缀时会抛出 `ValueError`。前缀必须非空且不含空白。带媒体的 prompt 不会作为命令。
命令调用工具时和模型调用工具一样发出 `ToolCallStarted` 和 `ToolCallFinished`，因此工具处理器同样作用于它们；其
`ToolContext` 的 `command` 为真。
`agent.run_command(tape, line)` 可以在已打开的 tape 上直接执行命令。

## 事件

循环通过 `bub.core.events` 中的事件报告进度；注册处理器即可观察或干预它。

```python
from bub.core.events import ToolCallFinished, ToolCallStarted, StepFinished, ToolCallDecision


@agent.events.on(ToolCallStarted)
def guard(event: ToolCallStarted) -> None:
    if event.call.tool == "lookup_order" and not event.call.arguments.get("order_id"):
        event.decision = ToolCallDecision.deny("order_id is required")


@agent.events.on(ToolCallFinished)
async def audit(event: ToolCallFinished) -> None:
    print("tool", event.call.tool, event.result.duration_ms, "ms")


agent.events.on(StepFinished, lambda event: print("step", event.step, event.status))
```

| 事件                           | 时机                                         | 处理器可以                                                                                                        |
| ------------------------------ | -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `RunStarted`                   | 一次运行开始                                 | 观察模型 spec、prompt 和运行 metadata                                                                             |
| `RunFinished`                  | 运行的 stream 关闭                           | 观察状态（`ok`、`error` 或 `cancelled`）、步数、耗时和错误                                                        |
| `StepStarted` / `StepFinished` | 一个模型步骤开始 / 以某个状态结束            | 观察                                                                                                              |
| `LLMCallStarted`               | 每次模型调用之前                             | 替换 `event.request`（模型、消息、`max_tokens`），或设置 `event.decision = LLMCallDecision.finish(text)` 跳过调用                   |
| `LLMCallFinished`              | 每次模型调用完成或失败之后                   | 观察文本、工具调用、用量和错误                                                                                    |
| `ToolCallStarted`              | 每次工具调用之前                             | 替换 `event.call` 以修改参数，或设置 `ToolCallDecision.replace(result)` / `deny(message)`                         |
| `ToolCallFinished`             | 每次工具调用之后，包括被拒绝和被替换的调用   | 赋值 `event.result.result` 以改变模型看到的结果                                                                   |
| `ChatFinished`                 | 一个步骤的对话已可记录                       | 观察                                                                                                              |
| `CommandFinished`              | 一个命令 prompt 执行完毕（见 [命令](#命令)） | 观察命令、状态和输出                                                                                              |

`agent.events.on(EventType, handler)` 注册同步或异步处理器并返回它；`@agent.events.on(EventType)` 以装饰器形式完成同样的注册，
`agent.events.off(EventType, handler)` 将其移除。为 `AgentEvent` 注册的处理器接收所有事件。处理器按注册顺序执行；
在 `Before*` 事件上设置 decision 会停止向后续处理器分发。处理器抛出的异常会传给本次运行。

每个事件都带有本次运行的 `state`；循环事件还带有它的 `tape`。记录本身也是一个处理器：最先注册的 `TapeRecorder`
把对话和 `loop.*` 事件写入本次运行的 tape。

### 自定义事件

可以定义自己的事件，让工具、处理器和应用通过同一条事件总线协作。事件是继承 `AgentEvent` 的 keyword-only dataclass
（继承 `TapeEvent` 则还会携带本次运行的 `tape`）。用 `await events.emit(event)` 发出事件：它会把事件分发给为该类型或其
父类型注册的处理器，并返回该事件。在工具中，本次运行的事件总线是 `context.events`：

```python
from dataclasses import dataclass

from bub.core.events import AgentEvent
from bub.core.tools import ToolContext


@dataclass(kw_only=True)
class OrderLookedUp(AgentEvent):
    order_id: str
    status: str


@dataclass(kw_only=True)
class RefundRequested(AgentEvent):
    order_id: str
    approved: bool | None = None

    @property
    def stopped(self) -> bool:
        # 一旦有处理器做出决定，后续处理器就不再执行。
        return self.approved is not None


async def lookup_order(order_id: str, *, context: ToolContext) -> dict[str, str]:
    """Look up the status of an order and whether it can be refunded."""
    result = {"order_id": order_id, "status": "shipped"}
    if context.events is not None:  # 工具在 agent 之外运行时为 None。
        await context.events.emit(OrderLookedUp(state=context.state, **result))
        request = await context.events.emit(RefundRequested(state=context.state, order_id=order_id))
        result["refundable"] = str(bool(request.approved))
    return result


agent.tools["lookup_order"] = Tool.from_callable(lookup_order, context=True)


@agent.events.on(OrderLookedUp)
async def notify(event: OrderLookedUp) -> None:
    print("order", event.order_id, "is", event.status, "for", event.state.get("user"))


@agent.events.on(RefundRequested)
def approve(event: RefundRequested) -> None:
    event.approved = event.state.get("user") == "Ada"
```

每个事件都带有发出它的那次运行的 `state`，因此请传入 `context.state`（在其他处理器中则传 `event.state`）。处理器可以修改
可变事件，发出方从返回的事件中读取结果；覆盖 `stopped` 可以在某个处理器做出决定后停止分发。应用也可以直接用
`await agent.events.emit(...)` 发出事件。自定义事件不会写入 tape；需要记录时，请在处理器中自行追加，例如在工具里调用
`await context.tape.append_event("order.looked_up", {...})`，或使用 `TapeEvent` 并在处理器中调用 `event.tape.append_event(...)`。

## 流式事件与生命周期

`agent.run()` 是一个异步上下文管理器：进入时开始运行，产出的流在迭代时驱动循环。事件包括 `text`、`reasoning`、`tool_call`、
`tool_result`、`usage`、`error` 和 `final`。`final` 事件结束的是一个模型步骤；流被消费完时整个运行结束。请检查 error
事件并处理迭代中抛出的异常。流对象还提供 `error` 和 `usage`。

离开 `async with` 块时会关闭流，提前 `break` 或抛出异常时也一样：模型流停止、tape fork 合并回去、trace span 结束。

```python
async with agent.run("Check order A123", model=model, tape="customer-42") as stream:
    async for event in stream:
        if event.kind == "tool_call":
            break  # 运行在这里停止，已记录的内容会保留。
```

如果要把流交给其他代码（例如稍后才消费它的框架），用相同参数调用 `await agent.start(...)`。它返回流但不负责关闭；
消费方必须把它消费完或调用 `aclose()`。

空 prompt 会直接得到 `error: empty prompt`，不会打开 tape。配置了 tracing 时，运行会记录为 `invoke_agent {name}` span；
见 [Observability](/zh-cn/docs/tutorials/observability/)。

如果要在自己管理的 tape 上驱动循环，用 `agent.session(name, state=...)` 打开它，再调用
`agent.run_on(tape, prompt, model=model)`；只跑一个模型步骤时用 `agent.run_once(prompt, model=model, tape=tape)`。
两者都接受 `options`。

## 定制循环

继承 `Agent` 可以改变每一步的准备方式。每个方法都会收到本次运行的 tape，其 `context.state` 就是 turn state：

| 方法                                   | 默认行为 | 用途                               |
| -------------------------------------- | -------- | ---------------------------------- |
| `prepare_tools(tape, prompt)`          | 全部工具 | 选择下一步模型可见的工具。         |
| `pending_messages(tape)`               | 无       | 返回运行期间到达的用户消息。       |
| `has_pending_messages(tape)`           | `False`  | 有消息等待时让运行再继续一步。     |
| `continue_prompt(tape, prompt, state)` | `None`   | 在工具调用后返回下一步的用户消息。 |

```python
from bub.core.tape import Tape


class TriageAgent(Agent):
    async def prepare_tools(self, tape: Tape, prompt):
        tools = await super().prepare_tools(tape, prompt)
        if tape.context.state.get("read_only"):
            tools = [tool for tool in tools if tool.name == "lookup_order"]
        return tools
```

要通过 HTTP 提供 agent 服务，请参阅 [用 FastAPI 提供 Bub agent 服务](/zh-cn/docs/tutorials/fastapi/)。
