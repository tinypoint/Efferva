# 架构与一致性语义

## 组件边界

```mermaid
flowchart LR
  I["Product Cookie / JWT / SSO"] --> R["IdentityResolver"]
  R --> B["Principal-scoped request"]
  B -->|"HTTP + durable SSE"| L["Service / Load Balancer"]
  L --> A1["Efferva instance A"]
  L --> A2["Efferva instance B"]
  A1 --> P[("PostgreSQL")]
  A2 --> P
  A1 -->|"stdio JSON-RPC proxy"| S1["Session sandbox<br/>Codex app-server"]
  A2 -->|"stdio JSON-RPC proxy"| S2["Session sandbox<br/>Codex app-server"]
  S1 --> W1[("Persistent Session volume<br/>workspace + CODEX_HOME")]
  S2 --> W2[("Persistent Session volume<br/>workspace + CODEX_HOME")]
```

App 实例包含无状态 HTTP 层与 Run worker。每个 Session 的 Codex app-server 与执行环境位于
同一个 sandbox；App 通过 Provider 的进程通道代理 JSON-RPC，浏览器永远不直连 sandbox。
PostgreSQL 是产品控制面真相来源；Codex 原生 Thread 状态与工作区文件一起位于 Session
持久卷，不挂载给 Web/App 实例。

## 产品身份与三层资源

| 概念 | 作用 | 持久化位置 |
|---|---|---|
| Principal | 当前产品用户、活动租户与即时能力 | 不持久化，由产品逐请求解析 |
| Session | 用户所有权、产品工作区与调度边界 | `app_sessions` |
| Thread | 同一工作区中的独立多轮对话 | `app_threads` + Session 卷内 Codex SQLite/rollout |
| Run | Thread 上的一次用户输入与执行 | `runs` + `run_events` + `messages` |

Principal 使用 `tenant_id + issuer + subject` 形成身份边界。Efferva 不复制产品用户表或角色
表；产品把现有角色即时映射为框架 Capability。Session 保存租户和所有者，Thread、Run、
Message 与 Event 均通过 Session 继承权限。

一个 Session 的多个 Thread 共用一个由 OpenSandbox 管理的持久工作区。多个 Thread
可以同时执行；同一 Thread 同一时刻只允许一个 Run 执行，避免 Codex 对话历史发生分叉写入。

## 授权边界

HTTP 层不能直接获取系统 Repository。每次已认证请求只能创建绑定 Principal 的
`AuthorizedRepository`，其每个 Session、Thread、Run 和 Event 查询都带租户/所有者作用域。
Worker 与沙盒控制器使用独立 `SystemRepository`，其中的实例 `owner_id` 只表示执行租约，
不是终端用户身份。

普通用户只有隐式 owner 权限。`SESSIONS_READ_TENANT` 与 `SESSIONS_WRITE_TENANT` 分别扩大
当前租户内的读、写作用域，永远不能跨租户。无权限的直接资源访问返回 `404`，防止利用响应
差异枚举资源；显式请求无权使用的 `scope=tenant` 返回 `403`。

## 为什么浏览器不需要粘性 Session

Run 在创建后由数据库队列驱动，与发起请求的 HTTP 连接解耦。每个 AG-UI 事件先提交到
`run_events(run_id, seq)`，再由任意 App 实例读取并输出 SSE。

浏览器断开只关闭当前 SSE reader，不会向 worker 发送取消。重连有两种方式：

1. AG-UI 客户端使用相同 `runId` 并发送 `Last-Event-ID`；
2. 内置 WebUI 重新读取 Thread，发现最近的 `queued/running` Run，然后建立 EventSource。

SSE 本身也使用同一个 AuthorizedRepository，因此补流不会绕开用户边界。负载均衡器可把
每次重连发到任意实例。

## 多实例执行租约

浏览器流量无粘性，但执行必须有单写者：

- `session_leases` 使一个 Session 在任一时刻归一个 App Runtime 管理；
- `fencing_epoch` 防止过期实例继续写 Run 事件；
- Session lease 保证同一持久卷上的 Codex app-server 只有一个 App owner；
- `FOR UPDATE SKIP LOCKED` 让多个实例竞争队列时只领取一次。

租约默认 30 秒、每 10 秒续约。同一 owner 可领取该 Session 的多个不同 Thread，所以共享
工作区并行成立；其他实例仍可服务这个 Session 的读取与 SSE。最后一个并行 Run 结束后，
Runtime 会 unsubscribe 并卸载 Codex Thread，再原子地释放空闲 Session 租约，因此下一次
Run 可由任意实例领取。

实例崩溃后，Run 会在租约过期后重新入队，并由其他实例从 PostgreSQL 恢复 Codex Thread。
MVP 的崩溃恢复语义是 **at-least-once**：如果旧实例在崩溃前已经对外部系统产生不可逆副作用，
重放 prompt 可能重复该副作用。浏览器断线不触发重放，因此正常断线补流没有这个问题。生产版
需要为有副作用的 Tool 增加幂等键或提交日志。

## PostgreSQL 与工作区存储的职责

PostgreSQL 保存身份、Session/Thread 映射、Run 队列和可补流事件。OpenSandbox 的持久卷保存
`/session/workspace` 与 `/session/codex-home`；后者是 Codex 原生 SQLite、rollout 与配置目录。
app-server 进程和 sandbox 计算实例都可丢弃，恢复时重新注入 Session 记录的 Runtime 并执行
`thread/resume`。

Runtime 二进制按 SHA 保存为 `/session/runtimes/<sha>/codex`。新 Session 固定当前 Wheel
版本；旧 Session 即使在 App 升级后，也继续从自己的持久卷启动原版本。

`workspace_bindings` 与 `sandbox_leases` 只保存 Provider 名称、不透明 `external_ref/state_json`
和 fencing 状态，不保存假设某种沙盒拓扑的 endpoint。Thread、Run 与 Event 仍通过 Session
继承身份边界，不重复存租户字段。

默认卷容量为 10Gi。Session 存在期间永久保留；显式删除后的保留策略默认 30 天。空闲
12 小时后只销毁 sandbox 计算实例，不删除持久卷。具体使用 Docker named volume、
Kubernetes PVC 或厂商存储由 OpenSandbox 部署决定。

## Sandbox 信任边界

Codex app-server 位于 sandbox，App 不需要 Docker Socket 或 Kubernetes 凭据。沙箱创建、
命令执行和文件访问统一通过 OpenSandbox Server；Docker Socket 或 Kubernetes 凭据只属于
OpenSandbox 自己的部署边界。标准端口的模型凭证默认由 OpenSandbox Credential Proxy 注入；本地
非标准端口代理可使用受控开发凭证。
