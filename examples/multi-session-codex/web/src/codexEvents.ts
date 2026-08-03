import type {
  CodexClient,
  CodexNotification,
} from "@efferva/codex-client";

import { asObject } from "./codexProjection";

export type CodexEvent = {
  sequence: number;
  threadId?: string;
  notification: CodexNotification;
};

type CodexEventSubscription = {
  after: number;
  threadId?: string;
  handler: (event: CodexEvent) => void;
};

function notificationThreadId(notification: CodexNotification): string | undefined {
  const params = asObject(notification.params);
  const turn = asObject(params.turn);
  const item = asObject(params.item);
  const value = params.threadId ?? turn.threadId ?? item.threadId;
  return value ? String(value) : undefined;
}

export class CodexEvents {
  private currentSequence = 0;
  private readonly activeTurns = new Map<string, CodexEvent[]>();
  private readonly subscriptions = new Set<CodexEventSubscription>();
  private unsubscribeNotification?: () => void;

  constructor(private readonly client: CodexClient) {}

  get sequence(): number {
    return this.currentSequence;
  }

  open(): void {
    if (this.unsubscribeNotification) return;
    this.unsubscribeNotification = this.client.onNotification((notification) => {
      this.publish(notification);
    });
  }

  subscribe(
    handler: (event: CodexEvent) => void,
    options: { after?: number; threadId?: string } = {},
  ): () => void {
    const subscription: CodexEventSubscription = {
      after: options.after ?? this.currentSequence,
      threadId: options.threadId,
      handler,
    };
    if (subscription.threadId) {
      for (const event of this.activeTurns.get(subscription.threadId) ?? []) {
        this.deliver(subscription, event);
      }
    }
    this.subscriptions.add(subscription);
    return () => {
      this.subscriptions.delete(subscription);
    };
  }

  close(): void {
    this.unsubscribeNotification?.();
    this.unsubscribeNotification = undefined;
    this.subscriptions.clear();
    this.activeTurns.clear();
  }

  private publish(notification: CodexNotification): void {
    const event: CodexEvent = {
      sequence: ++this.currentSequence,
      threadId: notificationThreadId(notification),
      notification,
    };
    if (event.threadId) {
      if (notification.method === "turn/started") {
        this.activeTurns.set(event.threadId, [event]);
      } else {
        this.activeTurns.get(event.threadId)?.push(event);
      }
    }
    for (const subscription of this.subscriptions) {
      this.deliver(subscription, event);
    }
    if (event.threadId && notification.method === "turn/completed") {
      this.activeTurns.delete(event.threadId);
    }
  }

  private deliver(subscription: CodexEventSubscription, event: CodexEvent): void {
    if (event.sequence <= subscription.after) return;
    if (subscription.threadId && subscription.threadId !== event.threadId) return;
    try {
      subscription.handler(event);
    } catch (cause) {
      console.error("Codex event subscriber failed", cause);
    }
  }
}
