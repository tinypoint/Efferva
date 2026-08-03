import { useEffect, useMemo } from "react";

import { createCodexClient } from "../CodexAgent";
import { CodexEvents } from "../codexEvents";

export function useCodexConnection(sessionId: string) {
  const connection = useMemo(() => {
    const client = createCodexClient(sessionId);
    return {
      client,
      events: new CodexEvents(client),
      closeTimer: undefined as ReturnType<typeof setTimeout> | undefined,
    };
  }, [sessionId]);

  useEffect(() => {
    if (connection.closeTimer !== undefined) {
      clearTimeout(connection.closeTimer);
      connection.closeTimer = undefined;
    }
    connection.events.open();
    return () => {
      connection.closeTimer = setTimeout(() => {
        connection.events.close();
        connection.client.close();
        connection.closeTimer = undefined;
      });
    };
  }, [connection]);

  return { client: connection.client, events: connection.events };
}
