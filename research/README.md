# Bub 项目研究文档

> 日期：2026-03-09（UTC+8）

## 📚 文档列表

| 文档 | 说明 |
|------|------|
| [architecture.md](./architecture.md) | 主要架构结构、模块间调用关系、核心流程 |
| [module-dependencies.md](./module-dependencies.md) | 模块依赖关系图（Mermaid） |
| [core-flow.md](./core-flow.md) | 核心流程详解（CLI 启动、Turn Orchestration、Agent 运行、通道处理等） |

---

## 🚀 快速理解

### 1. 架构分层

```
入口层 → 框架层 → Hook 层 → 内建实现层 → 工具/技能层
通道层独立，通过 provide_channels hook 与框架交互
```

### 2. 核心流程

1. **CLI 启动**：`bub run "hello"` → `BubFramework` → `process_inbound()`
2. **Turn Orchestration**：resolve_session → load_state → build_prompt → run_model → save_state → render_outbound → dispatch_outbound
3. **Agent 运行**：`Agent.run()` → `_run_command()` 或 `_agent_loop()`
4. **工具调用**：`tape.run_tools_async()` → LLM → 工具执行
5. **通道分发**：`ChannelManager.dispatch()` → `channel.send()`

### 3. 关键设计

- **Hook-first**：所有扩展点通过 hook 实现
- **异步支持**：大部分流程支持 async/await
- **防抖机制**：通道消息支持防抖和批量处理
- **Tape 记录**：完整记录会话、工具调用、事件
- **技能发现**：多路径技能发现，支持覆盖

---

## 📖 阅读建议

### 首次阅读

1. 先读 [architecture.md](./architecture.md) 了解整体架构
2. 再读 [core-flow.md](./core-flow.md) 了解核心流程
3. 最后读 [module-dependencies.md](./module-dependencies.md) 了解模块依赖

### 深入理解

1. 阅读 `src/bub/framework.py` 了解 turn orchestration
2. 阅读 `src/bub/hookspecs.py` 了解 hook 契约
3. 阅读 `src/bub/builtin/hook_impl.py` 了解内建实现
4. 阅读 `src/bub/builtin/agent.py` 了解 Agent 运行

---

## 🔍 关键文件

| 文件 | 说明 |
|------|------|
| `src/bub/__main__.py` | CLI 入口 |
| `src/bub/framework.py` | 框架核心（turn orchestration） |
| `src/bub/hookspecs.py` | Hook 契约 |
| `src/bub/hook_runtime.py` | Hook 执行 |
| `src/bub/builtin/hook_impl.py` | 内建 hook 实现 |
| `src/bub/builtin/agent.py` | Agent 运行 |
| `src/bub/channels/manager.py` | 通道管理 |
| `src/bub/tools.py` | 工具注册 |
| `src/bub/skills.py` | 技能发现 |

---

## 🎯 扩展点

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

---

## 📝 笔记

### 设计亮点

1. **Hook-first**：通过 hook 机制实现扩展，核心稳定
2. **异步支持**：大部分流程支持 async/await
3. **防抖机制**：通道消息支持防抖和批量处理
4. **Tape 记录**：完整记录会话、工具调用、事件
5. **技能发现**：多路径技能发现，支持覆盖

### 待深入

1. Republic 的具体实现（LLM、Tape、Tool）
2. 插件加载机制（entry_points）
3. 通道实现细节（Telegram、CLI）
4. 工具调用的错误处理和重试机制

---

## 📞 相关资源

- [Bub 官方文档](https://bub.build)
- [Republic 项目](https://github.com/bubbuild/republic)
- [Tape 系统](https://tape.systems)
