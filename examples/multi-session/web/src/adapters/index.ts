import { ClaudeAdapter } from "./claude";
import { CodexAdapter } from "./codex";
import type { AgentAdapter, EngineId } from "../types";

export function createAdapter(engine: EngineId): AgentAdapter {
  return engine === "codex" ? new CodexAdapter() : new ClaudeAdapter();
}
