---
title: Sandbox providers
description: Understand the execution contract shared by Efferva sandbox backends.
sidebar:
  order: 6
  label: Sandbox providers
---

Sandbox providers implement lifecycle and runtime execution behind one contract. Codex remains outside the sandbox; only commands and filesystem operations cross the execution boundary.

Current provider work covers:

- OpenSandbox as the built-in integration
- Application-registered third-party providers

The conformance suite validates workspace persistence, streaming execution, stdin, concurrent processes, termination, file operations, and stop/start behavior.

:::caution[Preview surface]
The provider SDK is implemented but its public authoring guide is not yet stable. Expect documentation changes before v0.1 is published.
:::
