---
title: Agent 循环
description: 了解 bub.core.Agent 核心循环机制，掌握 Run、Step、流式事件与 Tape 记忆管理。
---

`bub.core.Agent` 是 `bub` 的最小内核，负责驱动大模型与本地工具之间的交互循环。你可以直接将它嵌入任何 Python 服务、后端框架或自定义脚本中。

在 [快速上手](/zh-cn/docs/getting-started/quickstart/) 中我们已经体验过如何初始化并运行一个 Agent：

```py
import asyncio
import republic
from bub.core import Agent


async def main() -> None:
    # 初始化核心 Agent，指定系统提示词
    agent = Agent(system_prompt="Be concise.")

    # 使用 republic 获取底层大模型连接
    async with republic.get_model("openai:gpt-5-mini") as model:
        # 启动一次 Run，指定对话 Tape 名称
        async with agent.run("What can you do?", model=model, tape="demo") as stream:
            async for event in stream:
                if event.kind == "text":
                    print(event.data.get("delta", ""), end="", flush=True)


asyncio.run(main())
```

本文将为你介绍 `bub.core.Agent` 背后的一些细节。

## 1. Run 与 Step {#run-and-step}

与传统的单次 API 请求不同，Agent 的本质是一个**多步交互循环**（Loop）：它接收用户输入向模型发问，如果模型请求调用工具，Agent 会在本地执行工具，并将结果打包带回给模型，直到模型吐出最终答复。

为了精确掌控这个过程，`bub` 在架构上明确区分了两个层级：

- **Run**：一次完整的用户交互任务（由 `agent.run(...)` 触发）。
- **Step**：Run 内部的单轮迭代。**一个 Run 包含一个或多个 Step**。

下面是一次带有工具调用的多步循环流程：

```mermaid
flowchart TD
    Start["进入 agent.run()，初始化 Tape 分支"] --> Consume["调用方通过 async for 迭代事件流"]
    Start --> RunStarted["发出 RunStarted"]
    Consume --> Step["Step 启动：向模型发送上下文"]
    Step --> Output["产出 reasoning / text / usage"]
    Output --> Decision{"模型是否请求工具？"}
    Decision -->|"是"| Call["产出 tool_call，执行本地工具函数"]
    Call --> Result["产出 tool_result 与单步 final"]
    Result --> Step
    Decision -->|"否"| Final["产出最终文本与 final"]
    Final --> Close["关闭流，发出 RunFinished，合并 Tape 分支"]
```

在上面的最小示例中，由于没有挂载工具，模型直接生成文本，整个 Run 仅需 1 个 Step 即可结束。当引入工具后，多个 Step 会在底层自动串联。

---

## 2. 消费事件流 {#consume-the-stream}

进入 `agent.run()` 后，`bub` 会初始化会话并发出 `RunStarted`，但底层的模型与工具循环采用了**惰性求值（Lazy Evaluation）**设计：只有当你通过消费事件流时，Agent 才会真正开始步进和工作。仅仅创建上下文而不消费流，任务将不会执行。下面是一个简单的例子：

```python
async with agent.run(prompt, model=model, tape="quickstart") as stream:
    async for event in stream:
        ...
```

始终建议将 `agent.run()` 放在 `async with` 上下文管理器中，这样可以确保无论是因为正常跑完、中途异常退出还是被外部取消，底层连接和流都能被安全清理。未正常跑完的 Run 会以 `cancelled` 状态收尾。

所有事件你均可以通过 `event.kind` 来获取。

:::caution
当出现 `final` 事件时，仅表示**当前 Step 的结束**，而不是整个 Run 的终点。
如果模型在当前轮次请求调用了工具，工具执行完毕后，Agent 必然会带着工具结果进入下一轮 Step，并产生新的事件。因此，无论你是向客户端返回流式响应，还是统计任务完成状态，都必须**等待整个 `async for event in stream` 循环完整结束**才开始执行。
:::

## 3. 事件类型与数据 {#stream-events}

在迭代 `stream` 时，你会收到不同 `kind` 的事件。每个事件的 `event.data` 数据结构如下：

| 事件类型 | `event.data` 示例 | 说明 |
| --- | --- | --- |
| `reasoning` | `{"delta": "..."}` | 模型思考过程（Chain of Thought / Reasoning）的文本增量 |
| `text` | `{"delta": "..."}` | 面向用户的正式回复文本增量 |
| `tool_call` | `{"tool_calls": [{"id", "name", "arguments"}, ...]}` | 模型请求调用的工具列表（`arguments` 为 JSON 字符串） |
| `tool_result` | `{"tool_results": [...]}` | 与工具调用顺序对应的执行结果，异常信息也在此回传给模型 |
| `usage` | `{"usage": {...}, "elapsed_seconds": ...}` | 当前 Step 的模型 Token 消耗量与耗时统计 |
| `final` | `{"ok": True, "text": ...}` 或工具汇总 | 当前 Step 的汇总结果 |

因为，在宿主程序中，通常需要根据 `event.kind` 分流处理，这也就是为什么叫 Agent 循环，因为本质上就是在一个循环中不断分流处理事件：

```python
async for event in stream:
    if event.kind == "tool_call":
        for call in event.data["tool_calls"]:
            print(f"[{event.step_index}] 模型请求调用工具：{call['name']}")
    elif event.kind == "text":
        print(event.data["delta"], end="", flush=True)
```

## 4. Tape 与记忆分支管理 {#tape-management}

细心的你会发现在开头的示例中传入了 `tape="demo"`。

在 Bub 的架构设计中，**Tape** 是核心的对话历史与状态存储载体，它可以：

- **基于记录恢复上下文**：不同于传统的内存会话状态（容易由于大模型上下文膨胀或丢失导致幻觉），`bub` 的上下文由 append-only 的 Tape 记录按需重构。
- **分支机制**：`tape="demo"` 允许本次运行挂载在特定的磁带分支上。流正常关闭时，分支中的记录默认会自动合并回主 Tape；若配置了参数丢弃分支记录，则不会污染主历史。

借助于 Tape 可以让我们 Agent 循环具备记忆和连续对话能力，当然在后面我们会单独对其进行[介绍](/zh-cn/docs/sdk/tape/#forks-and-merges)。
