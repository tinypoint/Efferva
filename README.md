# Efferva

Efferva 是可嵌入产品 FastAPI 应用的多租户云端 Agent 框架。产品继续拥有登录、用户和
组织体系；Efferva 负责身份作用域内的 Session、Thread、Run、沙盒、AG-UI、持久事件和
多实例执行租约。纯 Python Wheel 不携带自编译 Runtime；App 下载并校验固定版本的 OpenAI
官方 Codex，然后将 App Server 注入每个 Session 的持久沙盒。
