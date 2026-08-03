import { useEffect, useMemo } from "react";

import { createCodexClient } from "../CodexAgent";
import { CodexEvents } from "../codexEvents";

export function useCodexConnection(sessionId: string) {
  const connection = useMemo(() => {
    const client = createCodexClient(sessionId);
    return {
      client,
      events: new CodexEvents(client),
    };
  }, [sessionId]);

  useEffect(() => {
    connection.events.open();
    return () => {
      connection.events.close();
      connection.client.close();
    };
  }, [connection]);

  return connection;
}
