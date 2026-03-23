# Bub 项目架构与核心流程研究

> 日期：2026-03-09（UTC+8）

## 1. 主要架构结构

### 1.1 整体分层

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLI / 入口层                                                       │
│  - src/bub/__main__.py (Typer CLI)                                 │
│  - src/bub/builtin/cli.py (内建命令)                                │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  框架编排层 (Framework)                                             │
│  - src/bub/framework.py (BubFramework)                             │
│    - process_inbound(): 单轮 turn orchestration                    │
│    - create_cli_app(): 组装 CLI                                    │
│    - get_channels(): 获取通道                                      │
│    - dispatch_via_router(): 出站消息路由                           │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Hook 协议与执行层                                                  │
│  - src/bub/hookspecs.py (BubHookSpecs)                             │
│    - resolve_session / load_state / build_prompt / run_model       │
│    - render_outbound / dispatch_outbound / register_cli_commands   │
│    - provide_channels / provide_tape_store / system_prompt         │
│  - src/bub/hook_runtime.py (HookRuntime)                           │
│    - call_first / call_many / notify_error                         │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  内建实现层 (builtin)                                               │
│  - src/bub/builtin/hook_impl.py (BuiltinImpl)                      │
│    - 实现上述 hookspec 的默认行为                                   │
│  - src/bub/builtin/agent.py (Agent)                                │
│    - 运行模型、工具调用、tape 管理                                  │
│  - src/bub/builtin/tools.py (内建工具)                              │
│    - bash / fs.* / tape.* / web.fetch / help 等                    │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  通道层 (Channels)                                                  │
│  - src/bub/channels/manager.py (ChannelManager)                    │
│    - 监听消息、调度处理、出站路由                                   │
│  - src/bub/channels/handler.py (BufferedMessageHandler)            │
│    - 消息缓冲、防抖、批量处理                                       │
│  - src/bub/channels/message.py (ChannelMessage)                    │
│    - 消息结构定义                                                   │
│  - src/bub/channels/telegram.py / cli/*                            │
│    - 具体通道实现                                                   │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  工具与技能层                                                       │
│  - src/bub/tools.py (工具注册与调用)                                │
│  - src/bub/skills.py (技能发现与加载)                               │
│  - src/bub_skills/* (内置技能)                                     │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  状态与上下文层                                                     │
│  - src/bub/builtin/context.py (tape 上下文)                         │
│  - src/bub/builtin/store.py (TapeStore 实现)                        │
│  - src/bub/builtin/tape.py (TapeService)                            │
│  - src/bub/builtin/settings.py (AgentSettings)                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.2 核心模块职责

| 模块 | 职责 |
|------|------|
| `__main__.py` | CLI 入口：创建 `BubFramework`，加载 hooks，组装 Typer app |
| `framework.py` | 框架核心：turn orchestration、hook 调度、通道路由 |
| `hookspecs.py` | Hook 契约：定义扩展点（`@hookspec`） |
| `hook_runtime.py` | Hook 执行：安全封装 pluggy 调用，支持 async/sync |
| `builtin/hook_impl.py` | 内建 hook 实现：默认行为（session、prompt、model、outbound 等） |
| `builtin/agent.py` | Agent：运行模型、工具调用、tape 管理 |
| `builtin/tools.py` | 内建工具：bash、fs、tape、web.fetch、help 等 |
| `channels/manager.py` | 通道管理：监听消息、调度处理、出站路由 |
| `channels/handler.py` | 消息缓冲：防抖、批量处理 |
| `channels/message.py` | 消息结构：ChannelMessage 定义 |
| `tools.py` | 工具注册：`@tool` 装饰器、工具调用日志 |
| `skills.py` | 技能发现：从 workspace、global、builtin 加载 SKILL.md |

---

## 2. 模块间调用关系

### 2.1 CLI 启动流程

```
__main__.py
  └─ create_cli_app()
       ├─ BubFramework()
       │    └─ load_hooks()
       │         ├─ _load_builtin_hooks()
       │         │    └─ register(BuiltinImpl)
       │         └─ load entry_points(group="bub")
       └─ framework.create_cli_app()
            ├─ Typer app
            └─ hook_runtime.call_many_sync("register_cli_commands", app=app)
                 └─ BuiltinImpl.register_cli_commands()
                      └─ app.command("run")(cli.run)
                          └─ cli.run()
                              └─ asyncio.run(framework.process_inbound(inbound))
```

### 2.2 单轮 turn orchestration

```
framework.process_inbound(inbound)
  ├─ hook_runtime.call_first("resolve_session")
  │    └─ BuiltinImpl.resolve_session()
  ├─ hook_runtime.call_many("load_state")
  │    └─ BuiltinImpl.load_state()
  ├─ hook_runtime.call_first("build_prompt")
  │    └─ BuiltinImpl.build_prompt()
  │         ├─ 若内容以 "," 开头 → 标记为 command
  │         └─ 否则拼接 context + content
  ├─ hook_runtime.call_first("run_model")
  │    └─ BuiltinImpl.run_model()
  │         └─ Agent.run()
  │              ├─ tape = tapes.session_tape(...)
  │              ├─ 若 prompt 以 "," 开头 → _run_command()
  │              └─ 否则 → _agent_loop()
  ├─ hook_runtime.call_many("save_state")
  │    └─ BuiltinImpl.save_state()
  ├─ _collect_outbounds()
  │    ├─ hook_runtime.call_many("render_outbound")
  │    │    └─ BuiltinImpl.render_outbound()
  │    └─ 若无 outbound → fallback 生成
  └─ hook_runtime.call_many("dispatch_outbound")
       └─ BuiltinImpl.dispatch_outbound()
            └─ framework.dispatch_via_router(message)
                 └─ ChannelManager.dispatch()
                      └─ channel.send(outbound)
```

### 2.3 Agent 运行流程

```
Agent.run(session_id, prompt, state)
  ├─ tape = tapes.session_tape(...)
  ├─ async with tapes.fork_tape(tape.name)
  │    ├─ tapes.ensure_bootstrap_anchor(tape.name)
  │    ├─ 若 prompt 以 "," 开头 → _run_command()
  │    │    ├─ 解析命令名与参数
  │    │    ├─ 若命令在 REGISTRY → 调用工具
  │    │    └─ 否则 → bash 执行
  │    └─ 否则 → _agent_loop()
  │         ├─ for step in 1..max_steps
  │         │    ├─ _run_tools_once()
  │         │    │    └─ tape.run_tools_async()
  │         │    │         ├─ system_prompt = framework.get_system_prompt()
  │         │    │         ├─ tools = model_tools(REGISTRY.values())
  │         │    │         └─ skills_prompt = discover_skills()
  │         │    └─ _resolve_tool_auto_result()
  │         │         ├─ kind="text" → 返回文本
  │         │         ├─ kind="continue" → 继续循环
  │         │         └─ kind="error" → 抛出异常
  │         └─ 若超出 max_steps → 抛出异常
```

### 2.4 通道管理流程

```
ChannelManager.listen_and_run()
  ├─ framework.bind_outbound_router(self)
  ├─ for channel in enabled_channels()
  │    └─ await channel.start(stop_event)
  ├─ while True
  │    ├─ message = await wait_until_stopped(messages.get(), stop_event)
  │    └─ task = asyncio.create_task(framework.process_inbound(message))
  └─ shutdown()
       ├─ 取消所有任务
       └─ await channel.stop()
```

---

## 3. 主要核心流程

### 3.1 CLI 命令执行流程

**入口**：`bub run "hello"`

1. `__main__.py` → `create_cli_app()`
2. `BubFramework.load_hooks()` 加载内建 + 插件 hooks
3. `framework.create_cli_app()` 组装 Typer app
4. 用户输入 `bub run "hello"`
5. `cli.run()` 构造 `ChannelMessage`
6. `asyncio.run(framework.process_inbound(inbound))`
7. 返回 `TurnResult`，打印 outbounds

### 3.2 普通文本处理流程

**场景**：用户输入普通消息（非逗号命令）

1. `build_prompt()` 拼接 context + content
2. `run_model()` 调用 Agent.run()
3. Agent._agent_loop() 循环调用工具/模型
4. `render_outbound()` 生成出站消息
5. `dispatch_outbound()` 发送到通道

### 3.3 逗号命令处理流程

**场景**：用户输入 `,help` 或 `,fs.read path=README.md`

1. `build_prompt()` 检测到以 "," 开头 → 标记为 command
2. `run_model()` 调用 Agent.run()
3. Agent._run_command() 解析命令名与参数
4. 若命令在 REGISTRY → 调用对应工具
5. 否则 → bash 执行
6. 返回命令输出

### 3.4 技能发现流程

**场景**：Agent 构建 system prompt

1. `discover_skills(workspace)`
   - 优先级：project → global → builtin
   - 读取 `SKILL.md` frontmatter
2. `render_skills_prompt(skills)`
3. 拼接到 system prompt

### 3.5 通道消息处理流程

**场景**：Telegram 消息到达

1. `ChannelManager.on_receive()`
2. 若 session_id 未处理过 → 创建 handler
3. 若通道需要防抖 → 使用 `BufferedMessageHandler`
4. 消息入队 → `messages.put(message)`
5. `listen_and_run()` 取出消息 → `framework.process_inbound()`
6. 出站消息通过 `ChannelManager.dispatch()` 发回通道

---

## 4. 关键数据流

### 4.1 消息结构

```python
ChannelMessage(
    session_id: str,
    channel: str,
    content: str,
    chat_id: str = "default",
    is_active: bool = False,
    kind: Literal["error", "normal", "command"],
    context: dict[str, Any],
    output_channel: str = "",
)
```

### 4.2 TurnResult

```python
TurnResult(
    session_id: str,
    prompt: str,
    model_output: str,
    outbounds: list[Envelope],
)
```

### 4.3 Hook 调用模式

- `call_first(hook_name, **kwargs)`：返回第一个非 None 结果
- `call_many(hook_name, **kwargs)`：返回所有结果列表
- `notify_error(stage, error, message)`：错误观察者

---

## 5. 扩展点（Hook 契约）

| Hook 名称 | 触发时机 | 返回值 |
|-----------|----------|--------|
| `resolve_session` | turn 开始 | session_id |
| `load_state` | turn 开始 | state dict |
| `build_prompt` | 构建 prompt | prompt string |
| `run_model` | 运行模型 | model output |
| `save_state` | turn 结束 | None |
| `render_outbound` | 生成出站消息 | list[Envelope] |
| `dispatch_outbound` | 发送出站消息 | bool |
| `register_cli_commands` | CLI 启动 | None |
| `on_error` | 错误发生 | None |
| `system_prompt` | 构建 system prompt | string |
| `provide_tape_store` | 提供 tape store | TapeStore |
| `provide_channels` | 提供通道列表 | list[Channel] |

---

## 6. 内置工具列表

| 工具名 | 说明 |
|--------|------|
| `bash` | 执行 shell 命令 |
| `fs.read` | 读取文件 |
| `fs.write` | 写入文件 |
| `fs.edit` | 编辑文件 |
| `skill` | 加载技能内容 |
| `tape.info` | 获取 tape 信息 |
| `tape.search` | 搜索 tape 条目 |
| `tape.reset` | 重置 tape |
| `tape.handoff` | 添加 handoff 锚点 |
| `tape.anchors` | 列出锚点 |
| `web.fetch` | 获取网页内容 |
| `help` | 显示帮助 |

---

## 7. 技能发现路径

1. `<workspace>/.agents/skills`
2. `~/.agents/skills`
3. `src/bub_skills`（内置）

每个技能目录必须包含 `SKILL.md`，支持 frontmatter：

```yaml
---
name: "skill-name"
description: "技能描述"
---
技能正文
```

---

## 8. 运行时配置

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `BUB_RUNTIME_ENABLED` | 是否启用 runtime | `auto` |
| `BUB_MODEL` | 模型 | `openrouter:qwen/qwen3-coder-next` |
| `BUB_API_KEY` | API 密钥 | None |
| `BUB_API_BASE` | API 基础 URL | None |
| `BUB_RUNTIME_MAX_STEPS` | 最大步数 | 8 |
| `BUB_RUNTIME_MAX_TOKENS` | 最大 token 数 | 1024 |
| `BUB_RUNTIME_MODEL_TIMEOUT_SECONDS` | 模型超时 | 90 |

---

## 9. 会话与 Tape

- 每个 session 对应一个 tape（会话记录）
- Tape 存储在 `~/.bub/tapes/`（默认）
- 支持 tape 查询、搜索、重置、handoff
- Tape 条目类型：message、tool_call、tool_result、anchor、event

---

## 10. 通道管理

- 支持多通道：Telegram、CLI
- 通道通过 `provide_channels` hook 提供
- `ChannelManager` 负责监听、调度、出站路由
- 支持防抖（debounce）和批量处理

---

## 11. 错误处理

- `hook_runtime.notify_error()` 调用 `on_error` hook
- 错误观察者可记录日志、发送通知
- 框架异常会记录到 tape event log

---

## 12. 总结

Bub 是一个 **hook-first** 的 AI 框架，核心是：

1. **小核心**：`framework.py` 负责 turn orchestration
2. **可扩展**：通过 hookspecs 定义扩展点，插件实现具体行为
3. **内建默认**：`builtin/hook_impl.py` 提供默认实现
4. **技能发现**：从 workspace、global、builtin 加载技能
5. **通道管理**：支持多通道、防抖、批量处理
6. **Tape 记录**：会话记录、工具调用、事件日志

通过 hook 机制，Bub 可以灵活扩展模型运行、工具调用、通道管理等能力，同时保持核心稳定。
