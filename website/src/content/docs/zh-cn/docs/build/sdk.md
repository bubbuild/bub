---
title: Python SDK
description: 在 Python 应用中嵌入 Bub，自定义工具、提示词、skills 和会话存储。
sidebar:
  order: 0
---

使用 `bub.builtin.Agent` 在应用内运行 builtin agent loop。
`BubFramework` 提供配置和 hooks，`Agent` 使用指定的工具与存储执行任务。

## 安装与配置

在 Python 3.12+ 项目中安装：

```bash
uv add bub
```

通过 `BUB_MODEL`、`BUB_API_KEY` 和可选的 `BUB_API_BASE`，或配置文件设置模型与凭据。
详见[配置参考](/zh-cn/docs/reference/settings/)。本文描述当前源码接口；
请使用包含这些接口的版本，或直接安装当前源码。

## 创建 Agent

将下面的示例保存为 `sdk_example.py`：

```python
import asyncio
from pathlib import Path

from bub import BubFramework, hookimpl
from bub.builtin import Agent
from bub.builtin.hook_impl import BuiltinImpl
from bub.builtin.tools import skill_describe
from bub.store import FileTapeStore
from bub.tools import Tool


class ApplicationHooks(BuiltinImpl):
    def __init__(self, framework: BubFramework, prompts: list[str]) -> None:
        super().__init__(framework)
        self.prompts = tuple(prompts)

    @hookimpl
    def system_prompt(self, prompt, state) -> str:
        return "\n\n".join(self.prompts)


async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up the status of an order."""
    # Replace this demonstration response with your application's order service.
    return {"order_id": order_id, "status": "shipped"}


def create_agent() -> tuple[BubFramework, Agent]:
    root = Path(__file__).resolve().parent
    framework = BubFramework(config_file=root / "config.yml")
    framework.workspace = root
    framework.plugin_manager.register(
        ApplicationHooks(framework, [
            "You are an order assistant. Reply directly to the user.",
            "Use lookup_order to check order status before answering.",
        ]),
        name="application",
    )
    agent = Agent(
        framework,
        tools=[Tool.from_callable(lookup_order), skill_describe],
        skill_dirs=[root / "skills"],
        tape_store=FileTapeStore(root / "sessions"),
    )
    return framework, agent


async def main() -> None:
    framework, agent = create_agent()
    async with framework.running():
        stream = await agent.run_stream(
            session_id="customer-42",
            prompt="Where is order A123?",
        )
        async for event in stream:
            if event.kind == "text":
                print(event.data.get("delta", ""), end="", flush=True)
            elif event.kind == "error":
                raise RuntimeError(str(event.data.get("message", "Agent failed")))
        print()


if __name__ == "__main__":
    asyncio.run(main())
```

配置好模型后，运行 `uv run python sdk_example.py`。
`config.yml` 不存在时也可以运行，环境变量配置仍然生效。
示例中的订单查询返回演示数据，接入应用时请替换为实际服务。

`ApplicationHooks` 覆盖 builtin 的系统提示方法，并继承其他 builtin hooks。
只注册这一个实例即可；这个示例中不要再调用 `load_builtin_hooks()` 或 `load_hooks()`。
系统提示 hooks 会累加，单独新增一个 prompt hook 会保留默认 builtin 的 channel 指令和工作区 `AGENTS.md` 内容。

如需 Bub 标准默认行为，创建 framework 后调用 `framework.load_builtin_hooks()`。
需要自动加载 `bub` entry-point group 下已安装的插件时，使用 `load_hooks()`。

## 工具、Skills 与会话

| 参数 | 行为 |
| --- | --- |
| `tools=[...]` | 接受 `Tool` 对象。`Tool.from_callable()` 从类型注解生成 schema，从 docstring 获取描述。 |
| `tools=None` | 构造时复制全局工具注册表。`tools=[]` 禁用工具。 |
| `skill_dirs=[Path(...)]` | 仅搜索这些目录，按顺序处理，同名 skill 使用第一个。 |
| `skill_dirs=None` | 搜索项目、用户和 builtin 目录。`skill_dirs=[]` 禁用发现。 |
| `tape_store=...` | 使用传入的 `TapeStore` 或 `AsyncTapeStore`。`FileTapeStore(path)` 将会话 tape 持久化到指定目录。 |
| `tape_store=None` | 使用 framework 当前的 store；没有活动 store 时使用实例独立的内存存储。 |

与 `@tool` 不同，`Tool.from_callable()` 不会把工具注册到全局表。
模型需要按需读取 skill 正文时，将 `skill_describe` 加入工具集合。
Skills 提供指令，不会自动授予 shell 或文件系统工具。

每个 skill 单独放在一个目录中：

```text
skills/
└── order-policy/
    └── SKILL.md
```

```markdown
---
name: order-policy
description: Rules for answering order-status questions.
---
Check the order service before giving a delivery status.
```

在相同 workspace 和 store 中复用 `session_id` 可以继续对话，新对话使用新 ID。
以 `temp/` 开头的会话运行在 fork 中，不会合并回父 tape。
归档和 builtin sidecar 的路径仍由 Bub 配置决定，独立于 `FileTapeStore` 的目录。

每轮调用可以缩小实例能力范围，并覆盖已保存的模型设置：

```python
stream = await agent.run_stream(
    session_id="customer-42",
    prompt="Use $order-policy to check order A123.",
    allowed_tools=["lookup_order", "skill"],
    allowed_skills=["order-policy"],
    model="provider:model-id",  # Replace with your configured provider/model.
    reasoning_effort="high",  # Use a value supported by that model.
)
```

必须消费返回的 stream。`allowed_tools` 支持工具原名和模型别名，例如 `fs.read` 与 `fs_read`。
显式 `model` 和 `reasoning_effort` 优先于已保存设置；示例中的模型标识需要替换成实际模型，
reasoning effort 也需使用该模型支持的值。
通常省略 `state`，由 framework 自动加载；传入字典会跳过加载，并在执行中修改该字典。
逗号开头的文本仍被解释为命令。命令直接使用实例工具集合，agent loop 的 `allowed_tools` 过滤不适合作为命令权限边界。

## 流式事件与生命周期

先 `await run_stream()`，再迭代其返回值。事件包括 `text`、`reasoning`、`tool_call`、
`tool_result`、`usage`、`error` 和 `final`。
`final` 表示一次模型步骤结束，整个 turn 需要等到迭代完成。
调用方应检查 error 事件并处理迭代抛出的异常。stream 对象还提供 `error` 和 `usage` 属性。

如果要提前退出已经开始的迭代，显式关闭迭代器：

```python
from contextlib import aclosing

stream = await agent.run_stream(session_id="customer-42", prompt="Check order A123")
async with aclosing(stream.__aiter__()) as events:
    async for event in events:
        print(event.kind, event.data)
```

所有 turn 完成之前，保持 `framework.running()` 开启。
在进入生命周期前访问 Agent 缓存的 `tape`，可能使其绑定到内存 fallback。
显式注入的 store 由应用管理生命周期。
`Agent` 本身不是异步上下文管理器，当前也没有 `run()` 便捷方法。

## 接入 FastAPI

运行 `uv add fastapi uvicorn` 安装服务依赖，然后在 `sdk_example.py` 旁保存 `app.py`：

```python
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from sdk_example import create_agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    framework, agent = create_agent()
    async with framework.running():
        app.state.agent = agent
        app.state.turn_lock = asyncio.Lock()
        yield


app = FastAPI(lifespan=lifespan)


class Task(BaseModel):
    session_id: str
    prompt: str


@app.post("/tasks")
async def run_task(task: Task, request: Request):
    async with request.app.state.turn_lock:
        stream = await request.app.state.agent.run_stream(
            session_id=task.session_id,
            prompt=task.prompt,
        )
        parts: list[str] = []
        error: str | None = None
        async for event in stream:
            if event.kind == "text":
                parts.append(str(event.data.get("delta", "")))
            elif event.kind == "error":
                error = str(event.data.get("message", "Agent failed"))
        if error is not None:
            raise HTTPException(status_code=502, detail=error)
        return {"session_id": task.session_id, "output": "".join(parts)}
```

使用 `uv run uvicorn app:app` 启动。这个示例在单进程内串行执行所有 turn。
需要不同会话并行时，可以由应用维护带清理机制的按会话锁。
多个 worker 共享 store 时需要跨进程协调，文件写锁并不能保证整个 turn 串行执行。

## Agent 与完整消息管线

直接调用 `Agent.run_stream()` 会运行 builtin loop，以及模型和工具拦截 hooks。
它不会调用 `build_prompt`、`save_state`、outbound 渲染或 channel 分发，
也不会选择插件替换的 `run_model` 实现。输出由应用消费。

需要完整消息管线或模型执行插件时，使用 `BubFramework.process_inbound()`。
它返回包含 `model_output`、`state` 和 `outbounds` 的 `TurnResult`；
`stream_output=True` 会通过绑定的 channel router 路由流式输出，但最终仍返回完整结果。
扩展点详见 [Hooks](/zh-cn/docs/build/hooks/)。

配置加载仍是进程级的，应在应用启动时统一配置。
工具、skills 和 store 按实例隔离，不代表配置文件和环境变量也按实例隔离。
