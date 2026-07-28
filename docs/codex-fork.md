# Codex 薄 fork 维护约定

`codex-fork` 只属于 AgentFrame 发布构建和维护工作区。产品使用方安装平台 Wheel，不 Clone
该仓库、不安装 Rust，也不在部署现场编译 Runtime。

## 原则

fork 只承载上游无法通过公开扩展点完成的能力。产品 API、Session/Run 模型、调度、AG-UI、
WebUI 与部署全部留在 `agent-framework`，避免把产品逻辑侵入 Codex。

当前 fork 只有两类语义变化：

1. App Server 启动时可注入 `Arc<dyn ThreadStore>`；
2. 非本地 ThreadStore 冷恢复时允许 `rollout_path = None`。

PostgreSQL Store 的具体实现位于 `agent-framework/crates/postgres-thread-store`，fork 不依赖
PostgreSQL 驱动。

Sandbox Provider 不增加 fork patch。AgentFrame 在沙盒外实现 Codex 已有的远程 exec-server
协议，并在 Executor Gateway 内转换为 `SandboxRuntime`；Docker/Kubernetes/E2B 等差异留在
Provider SDK。这样上游同步只需要长期维护 ThreadStore 注入，而不需要让 Codex 认识任何
沙盒厂商。

## Git remote

```bash
git remote -v
```

约定：

- `origin`：`tinypoint/codex`，允许 push；
- `upstream`：`openai/codex`，只 fetch，不 push。

## 同步上游

在没有未提交变更时：

```bash
git fetch upstream
git switch main
git rebase upstream/main
```

随后在 `agent-framework` 运行编译与集成测试。若上游重构 ThreadStore，应优先把注入点迁移到
新的稳定边界，不复制整个 App Server。

## 每次同步的验收顺序

```bash
cd codex-fork/codex-rs
just test -p codex-app-server

cd ../../agent-framework
cargo check --workspace
AGENTFRAME_TEST_DATABASE_URL=... uv run pytest -q -m integration
```

Codex 仓自身还要求 Rust 改动完成后执行项目级 `just fix -p codex-app-server` 与 `just fmt`。

## 降低冲突成本

- 注入接口放在 App Server 组合根，业务处理器只接收 trait object。
- 新行为用上游已有的 `ThreadStore` trait 表达，不修改协议数据结构。
- 每个 fork patch 都必须有独立的回归测试。
- 不把上游源码复制到产品仓；发布流水线固定经过验证的 fork revision，相邻仓路径只用于
  维护者构建。
