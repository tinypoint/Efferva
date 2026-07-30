# Codex 源码版本维护约定

`codex` 只属于 Efferva 发布构建和维护工作区。产品使用方安装平台 Wheel，不 Clone
该仓库、不安装 Rust，也不在部署现场编译 Runtime。

## 原则

Efferva 不再维护 Rust crate，也不向 Codex 注入 PostgreSQL ThreadStore。发布流水线直接
编译固定 revision 的 Codex CLI，并放入平台 Wheel；Session 启动时把该二进制注入 sandbox，
以 `codex app-server` 运行。Thread 原生状态保存在持久化 `CODEX_HOME`。

优先使用无补丁的上游源码。只有公开 app-server 协议确实无法表达的产品能力才允许进入
`tinypoint/codex`，且每个补丁必须能独立移除。产品 API、调度、身份、AG-UI 与 WebUI 永远
留在 Efferva。

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

随后由 AgentFrame 发布流水线构建平台 Wheel。若上游调整 app-server 协议，只更新 Python
适配层，不在框架仓复制 Rust 代码。

## 每次同步的验收顺序

```bash
cd codex/codex-rs
just test -p codex-app-server

cd ../../agent-framework
make wheel
```

Codex 仓自身还要求 Rust 改动完成后执行项目级 `just fix -p codex-app-server` 与 `just fmt`。

## 降低冲突成本

- 优先使用上游 app-server 协议和本地 `CODEX_HOME`。
- AgentFrame 仓不新增 Rust workspace 或 Codex wrapper crate。
- 每个 fork patch 都必须说明无法在框架层完成的原因。
- 不把上游源码复制到产品仓；发布流水线固定经过验证的 fork revision，相邻仓路径只用于
  维护者构建。
