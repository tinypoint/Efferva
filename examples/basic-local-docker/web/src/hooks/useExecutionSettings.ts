import { useCallback, useState } from "react";

import type {
  CollaborationMode,
  ExecutionSettings,
  ModelOption,
} from "../types";

type UseExecutionSettingsOptions = {
  threadId?: string;
  models?: ModelOption[];
};

export function useExecutionSettings({
  threadId,
  models,
}: UseExecutionSettingsOptions) {
  const [draftSettings, setDraftSettings] =
    useState<ExecutionSettings | null>(null);
  const [settingsByThread, setSettingsByThread] = useState<
    Record<string, ExecutionSettings>
  >({});
  const activeSettings = threadId
    ? settingsByThread[threadId]
    : draftSettings;
  const selectedModel =
    models?.find((item) => item.model === activeSettings?.model) ??
    models?.find((item) => item.isDefault) ??
    models?.[0];
  const model = selectedModel?.model ?? "";
  const reasoningEffort =
    selectedModel?.supportedReasoningEfforts.find(
      (item) => item.reasoningEffort === activeSettings?.reasoning_effort,
    )?.reasoningEffort ??
    selectedModel?.defaultReasoningEffort ??
    "low";
  const collaborationMode: CollaborationMode =
    activeSettings?.collaboration_mode === "plan" ? "plan" : "default";

  const selectModelAndEffort = useCallback(
    (nextModel: string, nextEffort: string) => {
      if (!threadId) {
        setDraftSettings((current) => ({
          model: nextModel,
          reasoning_effort: nextEffort,
          collaboration_mode: current?.collaboration_mode ?? "default",
        }));
        return;
      }
      setSettingsByThread((current) => ({
        ...current,
        [threadId]: {
          model: nextModel,
          reasoning_effort: nextEffort,
          collaboration_mode:
            current[threadId]?.collaboration_mode ?? "default",
        },
      }));
    },
    [threadId],
  );

  const onModelChange = useCallback(
    (nextModelId: string) => {
      const nextModel = models?.find((item) => item.model === nextModelId);
      if (!nextModel) return;
      selectModelAndEffort(nextModel.model, nextModel.defaultReasoningEffort);
    },
    [models, selectModelAndEffort],
  );

  const onReasoningEffortChange = useCallback(
    (nextEffort: string) => {
      selectModelAndEffort(model, nextEffort);
    },
    [model, selectModelAndEffort],
  );

  const onCollaborationModeChange = useCallback(
    (updatedThreadId: string, nextMode: CollaborationMode) => {
      if (updatedThreadId === "new") {
        setDraftSettings((current) => ({
          model: current?.model ?? model,
          reasoning_effort: current?.reasoning_effort ?? reasoningEffort,
          collaboration_mode: nextMode,
        }));
        return;
      }
      setSettingsByThread((current) => ({
        ...current,
        [updatedThreadId]: {
          model: current[updatedThreadId]?.model ?? model,
          reasoning_effort:
            current[updatedThreadId]?.reasoning_effort ?? reasoningEffort,
          collaboration_mode: nextMode,
        },
      }));
    },
    [model, reasoningEffort],
  );

  const loadThreadSettings = useCallback(
    (loadedThreadId: string, settings: ExecutionSettings) => {
      setSettingsByThread((current) => ({
        ...current,
        [loadedThreadId]: {
          ...settings,
          collaboration_mode: settings.collaboration_mode ?? "default",
        },
      }));
    },
    [],
  );

  const applyDraftToThread = useCallback(
    (createdThreadId: string) => {
      setSettingsByThread((current) => ({
        ...current,
        [createdThreadId]: {
          model,
          reasoning_effort: reasoningEffort,
          collaboration_mode: collaborationMode,
        },
      }));
      setDraftSettings(null);
    },
    [collaborationMode, model, reasoningEffort],
  );

  return {
    model,
    reasoningEffort,
    collaborationMode,
    onModelChange,
    onReasoningEffortChange,
    onCollaborationModeChange,
    loadThreadSettings,
    applyDraftToThread,
  };
}
