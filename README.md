# Efferva

Efferva 是可嵌入产品 FastAPI 应用的多租户云端 Agent 框架。产品继续拥有登录、用户和
组织体系；Efferva 负责身份作用域内的 Session 以及每个 Session 的持久 Sandbox。

产品部署时显式选择一个内核：

```python
from efferva import ClaudeCode, Codex, Efferva

Efferva(
    config=config,
    identity=resolve_principal,
    sandbox=sandbox_provider,
    engine=Codex(api_key=openai_api_key, base_url=openai_base_url),
).install(app, prefix="/agent")
```

Codex 使用官方 App Server WebSocket 协议；Claude Code 使用官方 Agent SDK 和 SSE。两个
内核都在 Session Sandbox 内运行，App 只做身份校验和协议代理，不维护另一套 Agent Run。
