---
title: Introduction
description: Start here to understand what Efferva owns and how it fits into an existing product.
sidebar:
  order: 1
  label: Introduction
---

Efferva is an embeddable, multi-tenant agent runtime for product backend teams. It installs into an existing FastAPI application and provides the durable control plane around an agent runtime.

:::note[Documentation preview]
This site is the new documentation skeleton for Efferva v0.1. The architecture is implemented, but the public guides are still being rewritten and expanded.
:::

## What Efferva owns

- Sessions, threads, runs, messages, and persistent events
- Codex Runtime lifecycle and agent execution
- Sandbox providers and per-session workspaces
- Multi-instance queues, leases, and fencing
- AG-UI and resumable SSE streams

## What your product owns

- Authentication and login state
- Users, organizations, and roles
- The FastAPI application and its middleware
- Product-specific permissions and business behavior

Continue with [Getting started](getting-started/) for the integration shape, or read [Core concepts](concepts/) for the runtime model.
