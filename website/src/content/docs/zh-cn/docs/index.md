---
title: 介绍
description: 从这里了解 Efferva 负责什么，以及它如何嵌入现有产品。
sidebar:
  order: 1
  label: 介绍
---

Efferva 是面向产品后端团队的可嵌入、多租户 Agent Runtime。它安装到现有 FastAPI 应用中，为 Agent Runtime 提供持久化控制面。

:::note[文档预览]
这是 Efferva v0.1 的新文档骨架。核心架构已经实现，但公开指南仍在重写和扩充。
:::

## Efferva 负责

- Session、Thread、Run、消息与持久事件
- Codex Runtime 生命周期与 Agent 执行
- 沙盒 Provider 与每个 Session 的工作区
- 多实例队列、租约与 fencing
- AG-UI 与可恢复的 SSE 事件流

## 你的产品负责

- 身份认证与登录态
- 用户、组织与角色
- FastAPI 应用及其中间件
- 产品权限和业务行为

继续阅读[快速开始](getting-started/)了解接入形态，或阅读[核心概念](concepts/)了解运行模型。
