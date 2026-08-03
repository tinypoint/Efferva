import {
  useCallback,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";

import { CodexAgent, type CodexRunConfig } from "../CodexAgent";
import type {
  CodexControl,
  CodexControlResult,
  CollaborationMode,
} from "../types";

export type GoalState = "off" | "pending" | "active";

type UseThreadControlsOptions = {
  agent: CodexAgent;
  desiredThreadIdRef: RefObject<string>;
  settingsRef: RefObject<CodexRunConfig>;
  goalState: GoalState;
  setGoalState: Dispatch<SetStateAction<GoalState>>;
  setError: Dispatch<SetStateAction<string | null>>;
  onCollaborationModeChange: (
    threadId: string,
    mode: CollaborationMode,
  ) => void;
};

export function useThreadControls({
  agent,
  desiredThreadIdRef,
  settingsRef,
  goalState,
  setGoalState,
  setError,
  onCollaborationModeChange,
}: UseThreadControlsOptions) {
  const setCollaborationMode = useCallback(
    async (target: CollaborationMode): Promise<boolean> => {
      const currentThreadId = desiredThreadIdRef.current;
      if (currentThreadId === "new") {
        onCollaborationModeChange(currentThreadId, target);
        setError(null);
        return true;
      }
      if (agent.isRunning) {
        setError(
          "Wait for the active turn to finish before changing thread state.",
        );
        return false;
      }
      try {
        const response = await agent.client.request<{
          data: Array<{
            name: string;
            mode?: string | null;
            model?: string | null;
            reasoning_effort?: string | null;
          }>;
        }>("collaborationMode/list", {});
        const preset = response.data.find(
          (item) =>
            item.name.toLocaleLowerCase() === target ||
            item.mode?.toLocaleLowerCase() === target,
        );
        if (!preset) throw new Error(`Codex does not provide ${target} mode`);
        const selectedModel =
          settingsRef.current.model || preset.model || undefined;
        if (!selectedModel) throw new Error(`${target} mode has no model`);
        await agent.client.request("thread/settings/update", {
          threadId: currentThreadId,
          collaborationMode: {
            mode: target,
            settings: {
              model: selectedModel,
              reasoning_effort:
                settingsRef.current.reasoningEffort ||
                preset.reasoning_effort ||
                null,
              developer_instructions: null,
            },
          },
        });
        onCollaborationModeChange(currentThreadId, target);
        setError(null);
        return true;
      } catch (cause) {
        setError(
          cause instanceof Error ? cause.message : "Failed to change mode",
        );
        return false;
      }
    },
    [agent, onCollaborationModeChange],
  );

  const setGoalMode = useCallback(
    async (enabled: boolean): Promise<boolean> => {
      if (enabled) {
        if (goalState !== "off") return true;
        if (
          settingsRef.current.collaborationMode === "plan" &&
          !(await setCollaborationMode("default"))
        ) {
          return false;
        }
        setGoalState("pending");
        setError(null);
        return true;
      }

      if (goalState !== "active") {
        setGoalState("off");
        setError(null);
        return true;
      }
      const currentThreadId = desiredThreadIdRef.current;
      if (currentThreadId === "new") {
        setGoalState("off");
        return true;
      }
      try {
        await agent.client.request("thread/goal/clear", {
          threadId: currentThreadId,
        });
        setGoalState("off");
        setError(null);
        return true;
      } catch (cause) {
        setError(
          cause instanceof Error ? cause.message : "Failed to clear goal",
        );
        return false;
      }
    },
    [agent, goalState, setCollaborationMode],
  );

  const runControl = useCallback(
    async (control: CodexControl): Promise<CodexControlResult | null> => {
      const currentThreadId = desiredThreadIdRef.current;
      if (control.action === "plan.toggle") {
        const target =
          settingsRef.current.collaborationMode === "plan" ? "default" : "plan";
        if (!(await setCollaborationMode(target))) return null;
        return {
          action: control.action,
          message:
            target === "plan" ? "Plan mode enabled." : "Plan mode disabled.",
          collaboration_mode: target,
        };
      }
      if (currentThreadId === "new") {
        setError("Send a message before using thread controls.");
        return null;
      }
      if (agent.isRunning) {
        setError(
          "Wait for the active turn to finish before changing thread state.",
        );
        return null;
      }
      try {
        let result: CodexControlResult;
        if (control.action === "goal.get") {
          const response = await agent.client.request<{
            goal: { objective: string; status: string } | null;
          }>("thread/goal/get", { threadId: currentThreadId });
          result = {
            action: control.action,
            message: response.goal
              ? `Goal: ${response.goal.objective} (${response.goal.status})`
              : "No goal is set.",
          };
        } else if (control.action === "goal.clear") {
          const response = await agent.client.request<{ cleared: boolean }>(
            "thread/goal/clear",
            { threadId: currentThreadId },
          );
          result = {
            action: control.action,
            message: response.cleared ? "Goal cleared." : "No goal was set.",
          };
        } else {
          const response = await agent.client.request<{
            goal: { objective: string; status: string };
          }>("thread/goal/set", {
            threadId: currentThreadId,
            ...(control.action === "goal.set"
              ? { objective: control.objective, status: "active" }
              : { status: control.status }),
          });
          result = {
            action: control.action,
            message:
              control.action === "goal.set"
                ? `Goal set: ${response.goal.objective}`
                : `Goal ${response.goal.status}: ${response.goal.objective}`,
          };
        }
        setError(null);
        return result;
      } catch (cause) {
        setError(
          cause instanceof Error ? cause.message : "Thread control failed",
        );
        return null;
      }
    },
    [agent, setCollaborationMode],
  );

  return { setCollaborationMode, setGoalMode, runControl };
}
