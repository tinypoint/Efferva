export type Session = {
  id: string;
  name: string;
  status: string;
  last_active_at: string;
  created_at: string;
};

export type ThreadSummary = {
  id: string;
  session_id: string;
  title: string;
  name?: string | null;
  preview?: string | null;
  workspace?: string | null;
  status?: {
    type: "active" | "idle" | "notLoaded" | "systemError";
    activeFlags?: string[];
  } | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type AgUiMessage = Message & {
  process?: Array<
    | { type: "reasoning"; text: string }
    | { type: "tool-call"; toolCallId: string }
  >;
  processDurationMs?: number;
};

export type ThreadDetail = ThreadSummary & {
  messages: AgUiMessage[];
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

export type CreateThreadInput = {
  workspace?: string;
  model?: string;
  reasoning_effort?: string;
};
import type { Message } from "@ag-ui/client";
