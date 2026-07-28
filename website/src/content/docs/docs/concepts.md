---
title: Core concepts
description: Learn the durable execution model behind Efferva sessions, threads, and runs.
sidebar:
  order: 3
  label: Core concepts
---

Efferva separates user identity, conversational state, agent execution, and workspace storage so each layer can scale independently.

## Principal

A product-authenticated actor scoped by `tenant_id`, `issuer`, and `subject`. Optional capabilities grant tenant-level administration without allowing cross-tenant access.

## Session

The durable identity and workspace boundary. A session belongs to one product principal and may contain multiple threads that share a workspace.

## Thread

An ordered conversation and history stream. Different threads in one session may run concurrently.

## Run

One agent execution within a thread. Runs in the same thread are serialized, while runs in different threads may proceed in parallel.

## Event

A persistent, ordered record written before it is streamed. Clients can resume from another application instance using `Last-Event-ID`.

## Sandbox

An isolated execution environment attached to a persistent session workspace through a provider contract.
