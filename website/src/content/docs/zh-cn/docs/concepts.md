---
title: 核心概念
description: 了解 Efferva 中 Session、Thread 与 Run 的持久化执行模型。
sidebar:
  order: 3
  label: 核心概念
---

Efferva 将用户身份、对话状态、Agent 执行与工作区存储分离，使各层能够独立扩展。

## Principal

由产品认证的参与者，通过 `tenant_id`、`issuer` 和 `subject` 限定作用域。可选 Capability 可以授予租户级管理能力，但绝不允许跨租户。

## Session

持久化身份与工作区边界。一个 Session 属于一个产品 Principal，可以包含多个共享工作区的 Thread。

## Thread

有序的对话和历史流。同一 Session 中不同 Thread 可以并行运行。

## Run

Thread 中的一次 Agent 执行。同一 Thread 的 Run 串行，不同 Thread 的 Run 可以并行。

## Event

先持久化、再输出的有序记录。客户端可以使用 `Last-Event-ID` 从另一应用实例恢复事件流。

## Sandbox

通过 Provider 契约连接到持久 Session 工作区的隔离执行环境。
