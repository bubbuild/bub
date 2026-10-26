---
title: 在 Python 中运行 Agent
description: 在 Python 程序中运行 Agent，添加工具，并管理会话和输出流。
---

`bub.core.Agent` 负责模型与工具之间的循环。你提供模型和工具，并决定会话如何保存、输出如何接入现有系统。

本页使用基础 SDK 安装，不需要 `app` 可选依赖。安装命令见[安装 SDK](/zh-cn/docs/getting-started/install/#agent-install)。需要配置、技能、渠道和插件发现时，使用 [Bub 应用](/zh-cn/docs/app/workspace/)。

## 运行一个带工具的 Agent {#basic-agent}

将以下代码保存为 `sdk_example.py`：

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

设置 OpenAI API 密钥并运行：

```bash
export REPUBLIC_OPENAI_API_KEY=sk-...
uv run python sdk_example.py
```

`lookup_order` 返回示例数据；接入实际系统时替换函数体。Agent 把工具结果交回模型，再输出回答。

`FileTapeStore` 将记录保存到 `sessions/`。再次使用 `tape="customer-42"` 会延续该会话，更换名称则开始另一个会话。省略 `store` 时使用内存存储。

## 消费输出流 {#stream-lifecycle}

`agent.run()` 是异步上下文管理器，进入后返回 `AsyncStreamEvents`。**迭代流才会驱动模型与工具循环**。模型不再请求工具且没有待处理消息时，运行结束。

一个 `final` 事件只表示一个模型 Step 结束；包含工具调用的 Step 之后可能还有下一个 Step。处理文本输出时，继续迭代直到流结束，不要在第一个 `final` 后停止。

`async with` 会在正常结束、异常或提前 `break` 时关闭流、释放模型连接，并合并本次运行的 Tape 分支。提前关闭会使未完成的 Run 以 `cancelled` 结束，已写入的记录默认仍保留，见[Tape 分支](/zh-cn/docs/concepts/tape/#forks)。

如果另一个组件负责消费输出，可以使用参数相同的 `await agent.start(...)` 取得流。接收方必须关闭它，包括尚未开始迭代的情况；自行消费时优先使用 `run()`。

流事件包括：

| `kind` | 内容 |
| --- | --- |
| `text` / `reasoning` | 文本或推理增量 |
| `tool_call` / `tool_result` | 工具调用与结果 |
| `usage` | 模型用量 |
| `error` | 错误载荷 |
| `final` | 当前模型 Step 结束 |

错误既可能表现为 `error` 事件，也可能在迭代时抛出异常。需要观察内部生命周期或拦截调用时，使用 [Agent 事件](/zh-cn/docs/concepts/events/)，不要只依赖输出流。

## 下一步 {#next-steps}

- [配置 Agent](/zh-cn/docs/sdk/options/)：模型、提示词和运行参数。
- [自定义工具与 code mode](/zh-cn/docs/sdk/tools/)：让 Agent 执行动作。
- [复用 Bub 应用](/zh-cn/docs/sdk/reuse-app/)：按深度接入插件工具、BubAgent 或完整 Turn。
- [FastAPI 服务](/zh-cn/docs/sdk/web-service/)：提供按会话隔离历史的 HTTP 接口。
