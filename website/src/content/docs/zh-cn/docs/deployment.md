---
title: 部署
description: Efferva 的部署目标与运行假设。
sidebar:
  order: 5
  label: 部署
---

Efferva 面向使用 Docker 本地运行或部署在 Kubernetes 上的产品应用。

## Docker

本地产品形态包括：

- 安装 Efferva Wheel 的 FastAPI 应用
- 作为持久化控制面存储的 PostgreSQL
- 支持的沙盒执行后端
- 注入每个 Session 沙箱的固定、已校验 OpenAI 官方 Codex

## Kubernetes

Kubernetes 拓扑由多个相同产品 Pod 连接 PostgreSQL。OpenSandbox 管理沙箱生命周期和工作区存储，Efferva 的租约与 fencing 保证跨实例执行的正确性。

:::note[指南状态]
生产 Manifest、Ready 检查、可观测性和升级流程将在公开发布前重写。
:::
