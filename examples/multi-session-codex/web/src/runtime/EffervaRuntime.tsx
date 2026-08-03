import { useMemo, type ReactNode } from "react";
import {
  CopilotChatConfigurationProvider,
  CopilotKitProvider,
} from "@copilotkit/react-core/v2";

import { AGENT_ID } from "./agentConfig";
import { RuntimeContext } from "./RuntimeContext";
import {
  useThreadRuntime,
  type ThreadRuntimeOptions,
} from "./useThreadRuntime";

type EffervaRuntimeProps = ThreadRuntimeOptions & {
  children: ReactNode;
};

export function EffervaRuntime({ children, ...options }: EffervaRuntimeProps) {
  const { agent, context } = useThreadRuntime(options);
  const agents = useMemo(() => ({ [AGENT_ID]: agent }), [agent]);

  return (
    <CopilotKitProvider agents__unsafe_dev_only={agents} showDevConsole={false}>
      <CopilotChatConfigurationProvider
        agentId={AGENT_ID}
        threadId={options.threadId ?? "new"}
        hasExplicitThreadId={false}
        labels={{
          chatInputPlaceholder:
            "Send a message… Use @ files, $ skills, or / commands",
          welcomeMessageText: "How can I help you today?",
        }}
      >
        <RuntimeContext.Provider value={context}>
          {children}
        </RuntimeContext.Provider>
      </CopilotChatConfigurationProvider>
    </CopilotKitProvider>
  );
}
