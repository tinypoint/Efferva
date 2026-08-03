import { createContext, useContext } from "react";
import type { Message } from "@ag-ui/client";

import type {
  CodexControl,
  CodexControlResult,
  CollaborationMode,
  FileSearchResult,
  ModelOption,
  SkillMetadata,
} from "../types";

type RuntimeContextValue = {
  sessionId: string;
  threadId: string;
  workspace?: string | null;
  loading: boolean;
  historyMessages: Message[];
  historyRevision: number;
  hasOlderHistory: boolean;
  loadingOlderHistory: boolean;
  loadOlderHistory: () => Promise<boolean>;
  searchFiles: (query: string) => Promise<FileSearchResult[]>;
  error: string | null;
  clearError: () => void;
  skills: SkillMetadata[];
  models: ModelOption[];
  model: string;
  reasoningEffort: string;
  collaborationMode: CollaborationMode;
  onModelChange: (model: string) => void;
  onReasoningEffortChange: (effort: string) => void;
  setCollaborationMode: (mode: CollaborationMode) => Promise<boolean>;
  goalMode: boolean;
  setGoalMode: (enabled: boolean) => Promise<boolean>;
  runControl: (control: CodexControl) => Promise<CodexControlResult | null>;
};

const RuntimeContext = createContext<RuntimeContextValue | null>(null);

function useEffervaRuntime(): RuntimeContextValue {
  const runtime = useContext(RuntimeContext);
  if (!runtime) throw new Error("EffervaChat must be inside EffervaRuntime");
  return runtime;
}

export { RuntimeContext, useEffervaRuntime };
export type { RuntimeContextValue };
