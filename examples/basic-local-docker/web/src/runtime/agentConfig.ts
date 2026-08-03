import { UseAgentUpdate } from "@copilotkit/react-core/v2";

export const AGENT_ID = "efferva";
export const AGENT_UPDATES = [
  UseAgentUpdate.OnMessagesChanged,
  UseAgentUpdate.OnStateChanged,
  UseAgentUpdate.OnRunStatusChanged,
];
