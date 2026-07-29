---
title: 沙盒 Provider
description: 了解 Efferva 沙盒后端共享的执行契约。
sidebar:
  order: 6
  label: 沙盒 Provider
---

沙盒 Provider 在统一契约背后实现生命周期与 Runtime 执行。Codex 保持在沙盒外，只有命令和文件系统操作跨越执行边界。

当前 Provider 工作覆盖：

- 内置的 OpenSandbox 集成
- 由应用注册的第三方 Provider

一致性测试覆盖工作区持久化、流式执行、stdin、并发进程、终止、文件操作以及 stop/start 行为。

:::caution[预览接口]
Provider SDK 已经实现，但公开编写指南尚未稳定。v0.1 发布前文档仍会调整。
:::
