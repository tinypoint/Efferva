export type Session = {
  id: string;
  name: string;
  status: string;
  last_active_at: string;
  created_at: string;
};

export type ThreadSummary = {
  id: string;
  sessionId: string;
  name?: string | null;
  preview?: string | null;
  cwd?: string | null;
  status?: {
    type: "active" | "idle" | "notLoaded" | "systemError";
    activeFlags?: string[];
  } | null;
  createdAt?: number | null;
  updatedAt?: number | null;
};

export type AgUiMessage = Message & {
  process?: Array<
    | { type: "reasoning"; text: string }
    | { type: "tool-call"; toolCallId: string }
  >;
  processDurationMs?: number;
};

export type ThreadHistoryPage = {
  messages: AgUiMessage[];
  next_cursor?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  collaboration_mode?: CollaborationMode | null;
  active_turn_id?: string | null;
  active_turn_started_at?: string | number | null;
  last_run_error?: string | null;
};

export type ReasoningEffortOption = {
  reasoningEffort: string;
  description: string;
};

export type ModelOption = {
  id: string;
  model: string;
  displayName: string;
  description: string;
  supportedReasoningEfforts: ReasoningEffortOption[];
  defaultReasoningEffort: string;
  isDefault: boolean;
};

export type ExecutionSettings = {
  model?: string | null;
  reasoning_effort?: string | null;
  collaboration_mode?: CollaborationMode | null;
};

export type CollaborationMode = "default" | "plan";

export type CodexControl =
  | { action: "plan.toggle" }
  | { action: "goal.get" }
  | { action: "goal.clear" }
  | { action: "goal.status"; status: "active" | "paused" }
  | { action: "goal.set"; objective: string };

export type CodexControlResult = {
  action: CodexControl["action"];
  message: string;
  collaboration_mode?: CollaborationMode | null;
};

export type SkillMetadata = {
  name: string;
  description: string;
  shortDescription?: string;
  scope: "user" | "repo" | "system" | "admin";
  enabled: boolean;
  path: string;
  interface?: {
    displayName?: string;
    shortDescription?: string;
  };
};

export type SkillListEntry = {
  cwd: string;
  skills: SkillMetadata[];
  errors: Array<{ path: string; message: string }>;
};

export type FileSearchResult = {
  root: string;
  path: string;
  match_type: "file" | "directory";
  file_name: string;
  score: number;
  indices?: number[] | null;
};

import type { Message } from "@ag-ui/client";
