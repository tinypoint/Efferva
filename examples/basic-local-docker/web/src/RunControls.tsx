import {
  createContext,
  useContext,
  useEffect,
  useRef,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";
import { useAui, useAuiState } from "@assistant-ui/react";

type RunControlsValue = {
  queuedMessages: string[];
  enqueue: (prompt: string) => void;
  removeQueued: (index: number) => void;
  steer: (prompt: string) => Promise<void>;
};

const RunControlsContext = createContext<RunControlsValue | null>(null);

export function useRunControls() {
  const value = useContext(RunControlsContext);
  if (!value) {
    throw new Error("Run controls must be used inside EffervaRuntime");
  }
  return value;
}

export function RunControlsProvider({
  queuedMessages,
  setQueuedMessages,
  steer,
  children,
}: {
  queuedMessages: string[];
  setQueuedMessages: Dispatch<SetStateAction<string[]>>;
  steer: (prompt: string) => Promise<void>;
  children: ReactNode;
}) {
  const value: RunControlsValue = {
    queuedMessages,
    enqueue(prompt) {
      const normalized = prompt.trim();
      if (!normalized) return;
      setQueuedMessages((current) => [...current, normalized]);
    },
    removeQueued(index) {
      setQueuedMessages((current) =>
        current.filter((_, itemIndex) => itemIndex !== index),
      );
    },
    steer,
  };

  return (
    <RunControlsContext.Provider value={value}>
      <QueuedMessageDispatcher
        queuedMessages={queuedMessages}
        setQueuedMessages={setQueuedMessages}
      />
      {children}
    </RunControlsContext.Provider>
  );
}

function QueuedMessageDispatcher({
  queuedMessages,
  setQueuedMessages,
}: {
  queuedMessages: string[];
  setQueuedMessages: Dispatch<SetStateAction<string[]>>;
}) {
  const aui = useAui();
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const dispatching = useRef(false);

  useEffect(() => {
    if (isRunning) {
      dispatching.current = false;
      return;
    }
    if (dispatching.current || queuedMessages.length === 0) return;

    dispatching.current = true;
    const next = queuedMessages[0]!;
    const draft = aui.composer.getState().text;
    setQueuedMessages((current) => current.slice(1));
    aui.composer.setText(next);
    queueMicrotask(() => {
      aui.composer.send();
      if (draft) {
        queueMicrotask(() => aui.composer.setText(draft));
      }
    });
  }, [aui, isRunning, queuedMessages, setQueuedMessages]);

  return null;
}
