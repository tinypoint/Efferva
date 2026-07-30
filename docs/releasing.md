# Python Wheel 发布

Efferva 发布一个与平台无关的纯 Python Wheel。Codex 不进入 Wheel，也不在发布流程中从
源码编译。

```bash
make wheel
```

产物写入 `dist/`。产品镜像安装这个 Wheel；应用启动时从 OpenAI 官方 GitHub Release 获取
框架固定的 Codex Linux musl 资产，验证内置 SHA-256 后缓存到 App 本地，再注入 Session
持久卷。

默认版本通过 `EFFERVA_CODEX_VERSION` 调整。选择非框架默认版本或自定义 target 时必须同时
提供 `EFFERVA_CODEX_ARCHIVE_SHA256`，避免把未经固定校验的网络内容作为可执行文件运行。
`EFFERVA_CODEX_RELEASE_TARGET` 仅作为高级跨架构覆盖项。

Session 在数据库记录 Codex 版本和解压后二进制 SHA，并把二进制保存为
`/session/runtimes/<sha>/codex`。因此 App 升级只影响新 Session；已有 Session 继续运行自己
固定的版本。

产品使用方不需要 `codex-fork`、Rust、Cargo、Runtime 路径或平台专用 Wheel。
