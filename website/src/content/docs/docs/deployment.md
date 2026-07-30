---
title: Deployment
description: Deployment targets and operating assumptions for Efferva.
sidebar:
  order: 5
  label: Deployment
---

Efferva targets product applications running locally with Docker or on Kubernetes.

## Docker

The local product shape combines:

- A FastAPI application with the Efferva wheel installed
- PostgreSQL as the durable control-plane store
- A supported sandbox execution backend
- A pinned, verified official Codex release injected into each Session sandbox

## Kubernetes

The Kubernetes topology runs multiple identical product pods against PostgreSQL. OpenSandbox owns sandbox lifecycle and workspace storage, while Efferva leases and fencing preserve execution correctness across instances.

:::note[Guide status]
Production manifests, readiness guidance, observability, and upgrade procedures are being rewritten before public release.
:::
