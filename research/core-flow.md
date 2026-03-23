# Bub 核心流程详解

> 日期：2026-03-09（UTC+8）

## 1. CLI 启动流程

### 1.1 命令行入口

```bash
bub run "hello"
```

### 1.2 执行路径

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

### 1.3 代码流程

```python
# __main__.py
def create_cli_app() -> typer.Typer:
    framework = BubFramework()
    framework.load_hooks()
    app = framework.create_cli_app()
    return app

# framework.py
def create_cli_app(self) -> typer.Typer:
    app = typer.Typer(name="bub", ...)
    @app.callback(invoke_without_command=True)
    def _main(ctx: typer.Context, workspace: str | None = ...) -> None:
        if workspace:
            self.workspace = Path(workspace).resolve()
        ctx.obj = self
    self._hook_runtime.call_many_sync("register_cli_commands", app=app)
    return app

# builtin/hook_impl.py
@hookimpl
def register_cli_commands(self, app: typer.Typer) -> None:
    from bub.builtin import cli
    app.command("run")(cli.run)
    app.command("chat")(cli.chat)
    app.command("hooks", hidden=True)(cli.list_hooks)
    app.command("message", hidden=True)(app.command("gateway")(cli.gateway))

# builtin/cli.py
def run(ctx: typer.Context, message: str, ...) -> None:
    framework = ctx.ensure_object(BubFramework)
    inbound = ChannelMessage(...)
    result = asyncio.run(framework.process_inbound(inbound))
    for outbound in result.outbounds:
        typer.echo(f"[{target_channel}:{target_chat}]\n{rendered}")
```

---

## 2. 单轮 Turn Orchestration

### 2.1 核心流程

```
framework.process_inbound(inbound)
  ├─ resolve_session()
  ├─ load_state()
  ├─ build_prompt()
  ├─ run_model()
  ├─ save_state()
  ├─ render_outbound()
  └─ dispatch_outbound()
```

### 2.2 详细步骤

#### 步骤 1：解析 Session ID

```python
# framework.py
async def process_inbound(self, inbound: Envelope) -> TurnResult:
    session_id = await self._hook_runtime.call_first(
        "resolve_session", message=inbound
    ) or self._default_session_id(inbound)
```

**Hook 实现**：

```python
# builtin/hook_impl.py
@hookimpl
def resolve_session(self, message: ChannelMessage) -> str:
    session_id = field_of(message, "session_id")
    if session_id is not None and str(session_id).strip():
        return str(session_id)
    channel = str(field_of(message, "channel", "default"))
    chat_id = str(field_of(message, "chat_id", "default"))
    return f"{channel}:{chat_id}"
```

#### 步骤 2：加载状态

```python
# framework.py
state = {"_runtime_workspace": str(self.workspace)}
for hook_state in reversed(
    await self._hook_runtime.call_many("load_state", message=inbound, session_id=session_id)
):
    if isinstance(hook_state, dict):
        state.update(hook_state)
```

**Hook 实现**：

```python
# builtin/hook_impl.py
@hookimpl
async def load_state(self, message: ChannelMessage, session_id: str) -> State:
    lifespan = field_of(message, "lifespan")
    if lifespan is not None:
        await lifespan.__aenter__()
    state = {"session_id": session_id, "_runtime_agent": self.agent}
    if context := field_of(message, "context_str"):
        state["context"] = context
    return state
```

#### 步骤 3：构建 Prompt

```python
# framework.py
prompt = await self._hook_runtime.call_first(
    "build_prompt", message=inbound, session_id=session_id, state=state
)
if not prompt:
    prompt = content_of(inbound)
```

**Hook 实现**：

```python
# builtin/hook_impl.py
@hookimpl
def build_prompt(self, message: ChannelMessage, session_id: str, state: State) -> str:
    content = content_of(message)
    if content.startswith(","):
        message.kind = "command"
        return content
    context = field_of(message, "context_str")
    context_prefix = f"{context}\n---\n" if context else ""
    return f"{context_prefix}{content}"
```

#### 步骤 4：运行模型

```python
# framework.py
model_output = ""
try:
    model_output = await self._hook_runtime.call_first(
        "run_model", prompt=prompt, session_id=session_id, state=state
    )
    if model_output is None:
        await self._hook_runtime.notify_error(...)
        model_output = prompt
    else:
        model_output = str(model_output)
finally:
    await self._hook_runtime.call_many(
        "save_state",
        session_id=session_id,
        state=state,
        message=inbound,
        model_output=model_output,
    )
```

**Hook 实现**：

```python
# builtin/hook_impl.py
@hookimpl
async def run_model(self, prompt: str, session_id: str, state: State) -> str:
    return await self.agent.run(session_id=session_id, prompt=prompt, state=state)
```

#### 步骤 5：保存状态

```python
# builtin/hook_impl.py
@hookimpl
async def save_state(self, session_id: str, state: State, message: ChannelMessage, model_output: str) -> None:
    tp, value, traceback = sys.exc_info()
    lifespan = field_of(message, "lifespan")
    if lifespan is not None:
        await lifespan.__aexit__(tp, value, traceback)
```

#### 步骤 6：渲染出站消息

```python
# framework.py
outbounds = await self._collect_outbounds(inbound, session_id, state, model_output)
```

**Hook 实现**：

```python
# builtin/hook_impl.py
@hookimpl
def render_outbound(
    self,
    message: Envelope,
    session_id: str,
    state: State,
    model_output: str,
) -> list[ChannelMessage]:
    outbound = ChannelMessage(
        session_id=session_id,
        channel=field_of(message, "channel", "default"),
        chat_id=field_of(message, "chat_id", "default"),
        content=model_output,
        output_channel=field_of(message, "output_channel", "default"),
        kind=field_of(message, "kind", "normal"),
    )
    return [outbound]
```

#### 步骤 7：分发出站消息

```python
# framework.py
for outbound in outbounds:
    await self._hook_runtime.call_many("dispatch_outbound", message=outbound)
```

**Hook 实现**：

```python
# builtin/hook_impl.py
@hookimpl
async def dispatch_outbound(self, message: Envelope) -> bool:
    content = content_of(message)
    session_id = field_of(message, "session_id")
    if field_of(message, "output_channel") != "cli":
        logger.info("session.run.outbound session_id={} content={}", session_id, content)
    return await self.framework.dispatch_via_router(message)
```

---

## 3. Agent 运行流程

### 3.1 Agent.run() 核心流程

```
Agent.run(session_id, prompt, state)
  ├─ tape = tapes.session_tape(...)
  ├─ async with tapes.fork_tape(tape.name)
  │    ├─ tapes.ensure_bootstrap_anchor(tape.name)
  │    ├─ 若 prompt 以 "," 开头 → _run_command()
  │    └─ 否则 → _agent_loop()
```

### 3.2 命令执行流程 (_run_command)

```
_run_command(tape, line)
  ├─ line = line[1:].strip()  # 去掉前导 ","
  ├─ name, arg_tokens = _parse_internal_command(line)
  ├─ context = ToolContext(...)
  ├─ 若 name 不在 REGISTRY → REGISTRY["bash"].run(cmd=line)
  │    └─ 执行 shell 命令
  ├─ 否则 → REGISTRY[name].run(...)
  │    ├─ 若工具需要 context → args.kwargs["context"] = context
  │    └─ 调用工具
  └─ 记录事件到 tape
```

**示例**：

```python
# 输入: ,fs.read path=README.md
# 解析: name="fs.read", arg_tokens=["path=README.md"]
# 调用: REGISTRY["fs.read"].run(path="README.md", context=context)
```

### 3.3 Agent 循环流程 (_agent_loop)

```
_agent_loop(tape, prompt)
  ├─ for step in 1..max_steps
  │    ├─ _run_tools_once(tape, next_prompt)
  │    │    └─ tape.run_tools_async(...)
  │    │         ├─ system_prompt = framework.get_system_prompt()
  │    │         ├─ tools = model_tools(REGISTRY.values())
  │    │         └─ skills_prompt = discover_skills()
  │    └─ _resolve_tool_auto_result(output)
  │         ├─ kind="text" → 返回文本
  │         ├─ kind="continue" → 继续循环
  │         └─ kind="error" → 抛出异常
  └─ 若超出 max_steps → 抛出异常
```

**关键点**：

1. `tape.run_tools_async()` 会调用 LLM，LLM 可能返回：
   - 纯文本响应
   - 工具调用请求
   - 工具调用结果

2. `_resolve_tool_auto_result()` 判断：
   - 若 LLM 返回纯文本 → 结束循环，返回文本
   - 若 LLM 请求工具调用 → 继续循环，执行工具
   - 若 LLM 返回错误 → 抛出异常

3. 循环最多执行 `max_steps` 次（默认 50）

### 3.4 工具调用流程

```
tape.run_tools_async()
  ├─ 构建 messages（包含 system prompt、用户消息）
  ├─ 调用 LLM
  ├─ LLM 返回 tool_calls 或 text
  ├─ 若有 tool_calls → 执行工具
  │    ├─ 从 REGISTRY 查找工具
  │    ├─ 调用工具 handler
  │    └─ 记录结果到 tape
  └─ 返回 ToolAutoResult
```

---

## 4. 通道消息处理流程

### 4.1 ChannelManager 启动

```
ChannelManager.listen_and_run()
  ├─ framework.bind_outbound_router(self)
  ├─ for channel in enabled_channels()
  │    └─ await channel.start(stop_event)
  ├─ while True
  │    ├─ message = await wait_until_stopped(messages.get(), stop_event)
  │    └─ task = asyncio.create_task(framework.process_inbound(message))
  └─ shutdown()
```

### 4.2 消息接收流程

```
ChannelManager.on_receive(message)
  ├─ channel = message.channel
  ├─ session_id = message.session_id
  ├─ 若 session_id 未处理过 → 创建 handler
  │    ├─ 若通道需要防抖 → BufferedMessageHandler
  │    └─ 否则 → 直接入队
  └─ await handler(message)
```

### 4.3 防抖处理流程 (BufferedMessageHandler)

```
BufferedMessageHandler.__call__(message)
  ├─ 若 message 以 "," 开头 → 直接处理（命令不防抖）
  ├─ 若 message 非 active 且超出 active_time_window → 忽略
  ├─ 否则 → 加入 pending_messages
  │    ├─ 若 message.active → 重置 debounce 定时器
  │    └─ 若无处理任务 → 创建处理任务
  └─ 处理任务：等待定时器 → 合并消息 → 调用 handler
```

**防抖参数**：

- `debounce_seconds`: 最小间隔（默认 1.0 秒）
- `max_wait_seconds`: 最大等待（默认 10.0 秒）
- `active_time_window`: 活动窗口（默认 60.0 秒）

---

## 5. 技能发现流程

### 5.1 技能发现路径

```
discover_skills(workspace_path)
  ├─ project: <workspace>/.agents/skills
  ├─ global: ~/.agents/skills
  └─ builtin: src/bub_skills/*
```

### 5.2 技能加载流程

```
discover_skills(workspace_path)
  ├─ for root, source in _iter_skill_roots(workspace_path)
  │    ├─ if not root.is_dir(): continue
  │    └─ for skill_dir in sorted(root.iterdir())
  │         ├─ if not skill_dir.is_dir(): continue
  │         └─ metadata = _read_skill(skill_dir, source=source)
  │              ├─ 读取 SKILL.md
  │              ├─ 解析 frontmatter (YAML)
  │              ├─ 验证 name、description
  │              └─ 返回 SkillMetadata
  └─ 返回按名称排序的技能列表
```

### 5.3 技能优先级

1. **project**（最高优先级）：`<workspace>/.agents/skills`
2. **global**：`~/.agents/skills`
3. **builtin**（最低优先级）：`src/bub_skills/*`

同名技能按优先级覆盖。

---

## 6. Tape 管理流程

### 6.1 Tape 创建流程

```
tapes.session_tape(session_id, workspace)
  ├─ workspace_hash = md5(workspace).hexdigest()[:16]
  ├─ session_hash = md5(session_id).hexdigest()[:16]
  └─ tape_name = f"{workspace_hash}__{session_hash}"
```

### 6.2 Tape 操作流程

```
tape.run_tools_async()
  ├─ 构建 messages
  ├─ 调用 LLM
  ├─ 记录到 tape
  │    ├─ message entries
  │    ├─ tool_call entries
  │    └─ tool_result entries
  └─ 返回结果
```

### 6.3 Tape 查询流程

```
tape.search(query, limit)
  ├─ 获取所有 message entries
  ├─ 对每个 entry：
  │    ├─ 若 query 在 payload 或 meta 中 → 加入结果
  │    └─ 否则 → 模糊匹配（rapidfuzz）
  └─ 返回最多 limit 个结果
```

---

## 7. 错误处理流程

### 7.1 框架错误处理

```
framework.process_inbound()
  ├─ try:
  │    └─ 正常流程
  └─ except Exception as exc:
       └─ await hook_runtime.notify_error(stage="turn", error=exc, message=inbound)
            └─ 调用所有 on_error hook 实现
```

### 7.2 Hook 错误处理

```
HookRuntime.notify_error()
  ├─ for impl in _iter_hookimpls("on_error")
  │    ├─ try:
  │    │    └─ impl.function(**call_kwargs)
  │    └─ except Exception:
  │         └─ logger.warning("hook.on_error_failed")
  └─ 错误观察者失败不影响主流程
```

### 7.3 工具调用错误处理

```
_run_command()
  ├─ try:
  │    └─ 调用工具
  └─ except Exception as exc:
       ├─ status = "error"
       ├─ output = f"{exc!s}"
       └─ 记录事件到 tape
```

---

## 8. 总结

### 8.1 核心流程链路

1. **CLI 启动**：`__main__.py` → `BubFramework` → `Typer app`
2. **消息处理**：`ChannelManager` → `framework.process_inbound()`
3. **Turn Orchestration**：resolve_session → load_state → build_prompt → run_model → save_state → render_outbound → dispatch_outbound
4. **Agent 运行**：`Agent.run()` → `_run_command()` 或 `_agent_loop()`
5. **工具调用**：`tape.run_tools_async()` → LLM → 工具执行
6. **通道分发**：`ChannelManager.dispatch()` → `channel.send()`

### 8.2 关键设计

1. **Hook-first**：所有扩展点通过 hook 实现
2. **异步支持**：大部分流程支持 async/await
3. **防抖机制**：通道消息支持防抖和批量处理
4. **Tape 记录**：完整记录会话、工具调用、事件
5. **技能发现**：多路径技能发现，支持覆盖

### 8.3 扩展点

| 扩展点 | 用途 |
|--------|------|
| `resolve_session` | 解析 session ID |
| `load_state` | 加载状态 |
| `build_prompt` | 构建 prompt |
| `run_model` | 运行模型 |
| `render_outbound` | 渲染出站消息 |
| `dispatch_outbound` | 分发出站消息 |
| `register_cli_commands` | 注册 CLI 命令 |
| `provide_channels` | 提供通道列表 |
| `system_prompt` | 提供 system prompt |
| `on_error` | 错误观察 |

通过这些扩展点，Bub 可以灵活扩展模型运行、工具调用、通道管理等能力。
