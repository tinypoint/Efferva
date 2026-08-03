# Python Wheel 发布

Efferva 发布一个与平台无关的纯 Python Wheel。Codex 不进入 Wheel、App 镜像或发布缓存，也不
从源码编译。

```bash
uv build --wheel --out-dir dist
```

产物写入 `dist/`。产品镜像只需要安装 Wheel，不需要额外安装 Codex。

Session 第一次连接时，框架在 Sandbox 内执行 `uname -m`，从 OpenAI 官方 GitHub Release
下载对应架构的固定 Codex Linux musl 资产，验证内置 SHA-256，并解压到
`/opt/efferva/runtimes/<version>/<target>/codex`。相同 Sandbox 后续直接复用该文件。

Sandbox 必须能够访问 GitHub，并提供 `sh`、`uname`、`tar`、`awk`，以及 `curl`、`wget` 或
Python 中至少一种下载方式。

产品使用方不需要 `codex-fork`、Rust、Cargo、Runtime 路径或平台专用 Wheel。
