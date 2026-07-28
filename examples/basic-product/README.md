# Basic Product

这个示例模拟一个已经拥有 Cookie 登录体系的产品。产品只负责把当前用户转换成
`Principal`，Session、Thread、Run、WebUI、沙盒和多实例状态都由 AgentFrame 提供。

```bash
uv run uvicorn --app-dir examples/basic-product main:app --reload
```

打开 <http://localhost:8000>，可以切换 Alice、Bob、Acme 只读管理员和另一个租户的管理员。
示例认证代码仅供观察集成边界，不能用于生产。
