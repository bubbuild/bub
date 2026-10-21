---
title: 基础 Agent
description: 在一个最小的 Python 程序里跑起 Agent，并控制它产生的输出流。
---

选择模型，加入工具，把 Agent 接到你的程序里。`bub.core.Agent` 负责模型与工具之间的循环；你决定如何保存会话，以及它怎样与你的系统协作。

这一页假设你已经按 Agent 的方式装好 Bub，见[安装 Bub](/zh-cn/docs/getting-started/install/#agent-install)。[Bub 应用](/zh-cn/docs/app/workspace/)建立在同一个内核上，额外提供配置和插件发现。

<a id="create-an-agent"></a>

## 运行示例

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

`agent.run()` 返回一个流，迭代这个流会推进执行：调用模型、执行模型请求的工具，再次调用模型，直到模型回答且不再请求工具。一个 `final` 事件表示一个模型步骤结束；完整消费流，才能完成这次运行。

用 `async with` 管理流。无论正常结束、抛出异常，还是提前 `break`，退出代码块都会关闭流、释放打开的模型流，并合并本次运行的 Tape 分支。运行失败也会保留已经写入的记录，详见[分支与部分结果](/zh-cn/docs/concepts/tape/#forks)。

如果另一个组件负责消费输出，可以用参数相同的 `await agent.start(...)` 取得流。接收它的组件必须负责关闭，包括尚未开始迭代的情况。自己消费输出时，优先使用 `run()`。

流事件包括 `text`、`reasoning`、`tool_call`、`tool_result`、`usage`、`error` 和 `final`。需要同时处理错误事件和迭代时抛出的异常。观察内部生命周期或拦截调用，请使用 [Agent 事件](/zh-cn/docs/concepts/events/)。

## 下一步

- [配置 Agent](/zh-cn/docs/sdk/options/)：模型、指令，以及 `Agent` 的其他参数。
- [工具与 CodeMode](/zh-cn/docs/sdk/tools/)：让 Agent 执行动作。
- [复用插件](/zh-cn/docs/sdk/reuse-plugins/)：把已有插件的工具和 hooks 搬进自己的 Agent。
- [示例：FastAPI 服务](/zh-cn/docs/sdk/web-service/)：把 Agent 放到 HTTP 端点后面。
