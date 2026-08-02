import {
  CodexClient,
  type CodexNotification,
  type CodexServerRequest,
} from "@efferva/codex-client";
import {
  AbstractAgent,
  type BaseEvent,
  type RunAgentInput,
} from "@ag-ui/client";
import { Observable, type Subscriber } from "rxjs";

import {
  asObject,
  projectToolCall,
  type JsonObject,
} from "./codexProjection";
import type { SkillMetadata, ThreadSummary } from "./types";

export type CodexRunConfig = {
  model?: string;
  reasoningEffort?: string;
  workspace?: string | null;
  skills: SkillMetadata[];
  collaborationMode: "default" | "plan";
};

type ResumeSource = {
  threadId: string;
  turnId: string;
};

type TurnStartResult = {
  turn: {
    id: string;
    startedAt?: number | null;
  };
};

type ThreadStartResult = {
  thread: ThreadSummary;
};

type ThreadResumeResult = {
  thread: ThreadSummary & {
    status?: { type?: string } | null;
  };
  model?: string;
  reasoningEffort?: string | null;
};

function event(value: JsonObject): BaseEvent {
  return value as BaseEvent;
}

class TurnTextProjection {
  readonly processMessageId: string;
  private processOpen = false;
  private processHasContent = false;
  private readonly processItems = new Set<string>();
  private readonly agentText = new Map<string, string>();
  private lastAgentId?: string;

  constructor(private readonly runId: string) {
    this.processMessageId = `${runId}:process`;
  }

  appendAgent(itemId: string, delta: string): BaseEvent[] {
    if (!delta) return [];
    this.agentText.set(itemId, (this.agentText.get(itemId) ?? "") + delta);
    this.lastAgentId = itemId;
    return this.appendProcess(`agent:${itemId}`, delta);
  }

  completeAgent(value: unknown): BaseEvent[] {
    const item = asObject(value);
    const itemId = String(item.id ?? crypto.randomUUID());
    const text = String(item.text ?? "");
    const streamed = this.agentText.get(itemId) ?? "";
    let events: BaseEvent[] = [];
    if (text && !streamed) {
      events = this.appendProcess(`agent:${itemId}`, text);
    } else if (text.startsWith(streamed) && text.length > streamed.length) {
      events = this.appendProcess(`agent:${itemId}`, text.slice(streamed.length));
    }
    this.agentText.set(itemId, text || streamed);
    this.lastAgentId = itemId;
    return events;
  }

  appendReasoning(itemId: string, delta: string): BaseEvent[] {
    return this.appendProcess(`reasoning:${itemId}`, delta);
  }

  closeProcess(): BaseEvent[] {
    if (!this.processOpen) return [];
    this.processOpen = false;
    return [
      event({
        type: "REASONING_MESSAGE_END",
        messageId: this.processMessageId,
      }),
    ];
  }

  finish(): BaseEvent[] {
    const events = this.closeProcess();
    const text = this.lastAgentId
      ? (this.agentText.get(this.lastAgentId) ?? "")
      : "";
    if (!text) return events;
    const messageId = `${this.runId}:assistant`;
    events.push(
      event({ type: "TEXT_MESSAGE_START", messageId }),
      event({ type: "TEXT_MESSAGE_CONTENT", messageId, delta: text }),
      event({ type: "TEXT_MESSAGE_END", messageId }),
    );
    return events;
  }

  private appendProcess(itemKey: string, delta: string): BaseEvent[] {
    if (!delta) return [];
    const events: BaseEvent[] = [];
    if (!this.processOpen) {
      this.processOpen = true;
      events.push(
        event({
          type: "REASONING_MESSAGE_START",
          messageId: this.processMessageId,
          role: "reasoning",
        }),
      );
    }
    if (!this.processItems.has(itemKey)) {
      this.processItems.add(itemKey);
      if (this.processHasContent) {
        events.push(
          event({
            type: "REASONING_MESSAGE_CONTENT",
            messageId: this.processMessageId,
            delta: "\n\n",
          }),
        );
      }
    }
    events.push(
      event({
        type: "REASONING_MESSAGE_CONTENT",
        messageId: this.processMessageId,
        delta,
      }),
    );
    this.processHasContent = true;
    return events;
  }
}

function promptFromInput(input: RunAgentInput): string {
  for (let index = input.messages.length - 1; index >= 0; index -= 1) {
    const message = input.messages[index];
    if (message?.role !== "user") continue;
    if (typeof message.content === "string") return message.content;
    if (Array.isArray(message.content)) {
      return message.content
        .filter((part) => part.type === "text")
        .map((part) => String(part.text ?? ""))
        .join("");
    }
  }
  throw new Error("A Codex turn requires a user message");
}

function userInputs(
  input: RunAgentInput,
  prompt: string,
  skills: SkillMetadata[],
): JsonObject[] {
  const items: JsonObject[] = [
    { type: "text", text: prompt, textElements: [] },
  ];
  const message = [...input.messages]
    .reverse()
    .find((candidate) => candidate.role === "user");
  if (message && Array.isArray(message.content)) {
    for (const part of message.content) {
      if (part.type !== "image" && part.type !== "audio") continue;
      const source = asObject(part.source);
      const value = String(source.value ?? "");
      if (!value) continue;
      items.push({
        type: part.type,
        url:
          source.type === "data"
            ? `data:${String(source.mimeType ?? "application/octet-stream")};base64,${value}`
            : value,
      });
    }
  }

  for (const match of prompt.matchAll(/(?<![\w@])@([^\s]+)/gu)) {
    const path = match[1];
    if (path) {
      items.push({
        type: "mention",
        name: path.split("/").filter(Boolean).at(-1) ?? path,
        path,
      });
    }
  }
  const skillsByName = new Map(
    skills
      .filter((skill) => skill.enabled)
      .map((skill) => [skill.name, skill] as const),
  );
  for (const match of prompt.matchAll(/(?<![\w$])\$([A-Za-z0-9:_-]+)/gu)) {
    const skill = skillsByName.get(match[1] ?? "");
    if (skill) items.push({ type: "skill", name: skill.name, path: skill.path });
  }
  return items;
}

function dynamicTools(tools: RunAgentInput["tools"]): JsonObject[] {
  return tools.map((tool) => {
    const value = asObject(tool);
    const fn = asObject(value.function);
    const source = Object.keys(fn).length ? fn : value;
    return {
      type: "function",
      name: String(source.name ?? ""),
      description: String(source.description ?? source.name ?? ""),
      inputSchema:
        source.inputSchema ??
        source.input_schema ??
        source.parameters ??
        { type: "object", properties: {} },
    };
  });
}

function shortThreadName(prompt: string): string {
  const firstLine = prompt.split(/\r?\n/u).find((line) => line.trim())?.trim() ?? "New thread";
  return firstLine.length > 36 ? `${firstLine.slice(0, 35).trim()}…` : firstLine;
}

async function defaultServerRequest(request: CodexServerRequest): Promise<unknown> {
  const params = asObject(request.params);
  if (request.method === "currentTime/read") {
    return { currentTimeAt: Math.floor(Date.now() / 1000) };
  }
  if (request.method === "item/tool/requestUserInput") {
    const questions = Array.isArray(params.questions) ? params.questions : [];
    return {
      answers: Object.fromEntries(
        questions
          .map(asObject)
          .filter((question) => question.id)
          .map((question) => [String(question.id), { answers: [] }]),
      ),
    };
  }
  if (
    request.method === "item/commandExecution/requestApproval" ||
    request.method === "item/fileChange/requestApproval"
  ) {
    return { decision: "decline" };
  }
  if (request.method === "mcpServer/elicitation/request") {
    return { action: "decline", content: null, _meta: null };
  }
  if (request.method === "item/permissions/requestApproval") {
    return { permissions: {}, scope: "turn" };
  }
  if (request.method === "item/tool/call") {
    return {
      contentItems: [
        {
          type: "inputText",
          text: `No browser handler is registered for ${String(params.tool ?? "dynamic tool")}.`,
        },
      ],
      success: false,
    };
  }
  throw new Error(`Unsupported Codex server request: ${request.method}`);
}

export function createCodexClient(sessionId: string): CodexClient {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return new CodexClient({
    url: `${protocol}//${window.location.host}/agent/api/sessions/${encodeURIComponent(sessionId)}/codex`,
    serverRequestHandler: defaultServerRequest,
  });
}

export class CodexAgent extends AbstractAgent {
  private resumeSource?: ResumeSource;
  private activeTurn?: ResumeSource;

  constructor(
    readonly client: CodexClient,
    private readonly getRunConfig: () => CodexRunConfig,
  ) {
    super({ agentId: "efferva", threadId: "new" });
  }

  setResumeSource(source: ResumeSource): void {
    this.resumeSource = source;
  }

  run(input: RunAgentInput): Observable<BaseEvent> {
    return this.observe((subscriber) => this.runTurn(input, subscriber));
  }

  protected override connect(input: RunAgentInput): Observable<BaseEvent> {
    const source = this.resumeSource;
    this.resumeSource = undefined;
    if (!source) return new Observable((subscriber) => subscriber.complete());
    return this.observe((subscriber) =>
      this.resumeTurn(input.runId, source, subscriber),
    );
  }

  async interrupt(): Promise<void> {
    if (!this.activeTurn) return;
    await this.client.request("turn/interrupt", {
      threadId: this.activeTurn.threadId,
      turnId: this.activeTurn.turnId,
    });
  }

  async steer(prompt: string): Promise<void> {
    if (!this.activeTurn) throw new Error("No Codex turn is active");
    await this.client.request("turn/steer", {
      threadId: this.activeTurn.threadId,
      expectedTurnId: this.activeTurn.turnId,
      clientUserMessageId: `efferva-steer-${crypto.randomUUID()}`,
      input: [{ type: "text", text: prompt, textElements: [] }],
    });
  }

  private observe(
    run: (subscriber: Subscriber<BaseEvent>) => Promise<void>,
  ): Observable<BaseEvent> {
    return new Observable<BaseEvent>((subscriber) => {
      let active = true;
      void run(subscriber)
        .then(() => {
          if (active) subscriber.complete();
        })
        .catch((cause) => {
          if (active) subscriber.error(cause);
        });
      return () => {
        active = false;
      };
    });
  }

  private async runTurn(
    input: RunAgentInput,
    subscriber: Subscriber<BaseEvent>,
  ): Promise<void> {
    const runId = input.runId;
    const prompt = promptFromInput(input);
    const config = this.getRunConfig();
    await this.client.connect();
    let threadId = this.threadId;
    subscriber.next(event({ type: "RUN_STARTED", runId, threadId }));
    subscriber.next(
      event({
        type: "STATE_SNAPSHOT",
        snapshot: {
          threadId,
          runId,
          turnId: null,
          status: "running",
          activities: {},
        },
      }),
    );

    try {
      if (threadId === "new") {
        const workspace = config.workspace || "/home/sandbox/workspace";
        await this.client.request("fs/createDirectory", {
          path: workspace,
          recursive: true,
        });
        const started = await this.client.request<ThreadStartResult>(
          "thread/start",
          {
            ...(config.model ? { model: config.model } : {}),
            cwd: workspace,
            approvalPolicy: "never",
            sandbox: "danger-full-access",
            historyMode: "paginated",
            dynamicTools: dynamicTools(input.tools),
          },
        );
        threadId = started.thread.id;
        this.threadId = threadId;
        const name = shortThreadName(prompt);
        const thread = { ...started.thread, name };
        subscriber.next(
          event({
            type: "RAW",
            event: {
              method: "efferva/thread-created",
              params: { thread },
            },
          }),
        );
        subscriber.next(
          event({
            type: "STATE_DELTA",
            delta: [{ op: "replace", path: "/threadId", value: threadId }],
          }),
        );
        await this.client.request("thread/name/set", { threadId, name });
      } else {
        await this.client.request("thread/resume", {
          threadId,
          excludeTurns: true,
        });
      }

      await this.waitForTurn(
        subscriber,
        runId,
        threadId,
        () =>
          this.client.request<TurnStartResult>("turn/start", {
            threadId,
            input: userInputs(input, prompt, config.skills),
            ...(config.model ? { model: config.model } : {}),
            ...(config.reasoningEffort
              ? { effort: config.reasoningEffort }
              : {}),
          }),
      );
    } catch (cause) {
      subscriber.next(
        event({
          type: "RUN_ERROR",
          code: "RUNTIME_ERROR",
          message: cause instanceof Error ? cause.message : String(cause),
        }),
      );
    }
  }

  private async resumeTurn(
    runId: string,
    source: ResumeSource,
    subscriber: Subscriber<BaseEvent>,
  ): Promise<void> {
    await this.client.connect();
    this.threadId = source.threadId;
    subscriber.next(
      event({
        type: "RUN_STARTED",
        runId,
        threadId: source.threadId,
      }),
    );
    subscriber.next(
      event({
        type: "STATE_SNAPSHOT",
        snapshot: {
          threadId: source.threadId,
          runId,
          turnId: source.turnId,
          status: "running",
          activities: {},
        },
      }),
    );
    await this.waitForTurn(
      subscriber,
      runId,
      source.threadId,
      async () => {
        const response = await this.client.request<ThreadResumeResult>("thread/resume", {
          threadId: source.threadId,
          excludeTurns: true,
        });
        return response.thread.status?.type === "active"
          ? { turn: { id: source.turnId } }
          : null;
      },
    );
  }

  private async waitForTurn(
    subscriber: Subscriber<BaseEvent>,
    runId: string,
    threadId: string,
    start: () => Promise<TurnStartResult | null>,
  ): Promise<void> {
    const projection = new TurnTextProjection(runId);
    const startedTools = new Set<string>();
    let turnId: string | undefined;
    let stepStarted = false;
    let settled = false;
    let resolveCompletion!: () => void;
    const completion = new Promise<void>((resolve) => {
      resolveCompletion = resolve;
    });
    const emit = (events: BaseEvent[]) => {
      for (const projected of events) subscriber.next(projected);
    };
    const finishStepStart = (id: string, startedAt?: number | null) => {
      if (stepStarted) return;
      stepStarted = true;
      turnId = id;
      this.activeTurn = { threadId, turnId: id };
      subscriber.next(
        event({
          type: "RAW",
          event: {
            method: "efferva/turn-started",
            params: { threadId, turnId: id, startedAt: startedAt ?? Date.now() },
          },
          turnId: id,
        }),
      );
      subscriber.next(event({ type: "STEP_STARTED", stepName: id }));
      subscriber.next(
        event({
          type: "STATE_DELTA",
          delta: [
            { op: "replace", path: "/turnId", value: id },
            {
              op: "add",
              path: "/startedAt",
              value: startedAt ?? Date.now(),
            },
          ],
        }),
      );
    };

    const unsubscribeNotification = this.client.onNotification((notification) => {
      const params = asObject(notification.params);
      if (params.threadId && String(params.threadId) !== threadId) return;
      if (turnId && params.turnId && String(params.turnId) !== turnId) return;
      this.projectNotification(
        notification,
        projection,
        startedTools,
        subscriber,
        (nativeTurnId, startedAt) => finishStepStart(nativeTurnId, startedAt),
        () => {
          settled = true;
          this.activeTurn = undefined;
          resolveCompletion();
        },
        runId,
        threadId,
      );
    });
    let rejectClose!: (error: Error) => void;
    const closed = new Promise<never>((_, reject) => {
      rejectClose = reject;
    });
    const unsubscribeClose = this.client.onClose((closeEvent) => {
      rejectClose(
        new Error(
          closeEvent.reason ||
            `Codex connection closed with code ${closeEvent.code}`,
        ),
      );
    });

    try {
      const result = await start();
      if (!result) {
        if (!settled) {
          subscriber.next(event({ type: "RUN_FINISHED", runId, threadId }));
        }
        return;
      }
      if (settled) return;
      finishStepStart(result.turn.id, result.turn.startedAt);
      if (!settled) await Promise.race([completion, closed]);
    } finally {
      unsubscribeClose();
      unsubscribeNotification();
      if (this.activeTurn?.turnId === turnId) this.activeTurn = undefined;
      emit(projection.closeProcess());
    }
  }

  private projectNotification(
    notification: CodexNotification,
    projection: TurnTextProjection,
    startedTools: Set<string>,
    subscriber: Subscriber<BaseEvent>,
    onStarted: (turnId: string, startedAt?: number | null) => void,
    onSettled: () => void,
    runId: string,
    threadId: string,
  ): void {
    const params = asObject(notification.params);
    const method = notification.method;
    if (method === "turn/started") {
      const turn = asObject(params.turn);
      if (turn.id) onStarted(String(turn.id), Number(turn.startedAt ?? Date.now()));
      return;
    }
    if (method === "item/agentMessage/delta") {
      for (const projected of projection.appendAgent(
        String(params.itemId ?? crypto.randomUUID()),
        String(params.delta ?? ""),
      )) {
        subscriber.next(projected);
      }
      return;
    }
    if (
      method === "item/reasoning/summaryTextDelta" ||
      method === "item/reasoning/textDelta"
    ) {
      for (const projected of projection.appendReasoning(
        String(params.itemId ?? crypto.randomUUID()),
        String(params.delta ?? ""),
      )) {
        subscriber.next(projected);
      }
      return;
    }
    if (method === "item/started") {
      const tool = projectToolCall(params.item);
      if (!tool) return;
      startedTools.add(tool.id);
      subscriber.next(
        event({
          type: "TOOL_CALL_START",
          toolCallId: tool.id,
          toolCallName: tool.name,
        }),
      );
      subscriber.next(
        event({ type: "TOOL_CALL_ARGS", toolCallId: tool.id, delta: tool.arguments }),
      );
      return;
    }
    if (method === "item/completed") {
      const item = asObject(params.item);
      const tool = projectToolCall(item);
      if (tool) {
        if (!startedTools.has(tool.id)) {
          subscriber.next(
            event({
              type: "TOOL_CALL_START",
              toolCallId: tool.id,
              toolCallName: tool.name,
            }),
          );
          subscriber.next(
            event({
              type: "TOOL_CALL_ARGS",
              toolCallId: tool.id,
              delta: tool.arguments,
            }),
          );
        }
        subscriber.next(event({ type: "TOOL_CALL_END", toolCallId: tool.id }));
        subscriber.next(
          event({
            type: "TOOL_CALL_RESULT",
            messageId: `${tool.id}:result`,
            toolCallId: tool.id,
            content: tool.result,
            role: "tool",
            ...(tool.isError
              ? { structuredContent: { error: tool.result }, isError: true }
              : {}),
          }),
        );
        startedTools.delete(tool.id);
        return;
      }
      if (item.type === "agentMessage") {
        for (const projected of projection.completeAgent(item)) {
          subscriber.next(projected);
        }
        return;
      }
    }
    if (method === "turn/completed") {
      const turn = asObject(params.turn);
      for (const projected of projection.finish()) subscriber.next(projected);
      const status = String(turn.status ?? "failed");
      subscriber.next(
        event({ type: "STEP_FINISHED", stepName: String(turn.id ?? "turn") }),
      );
      subscriber.next(
        event({
          type: "STATE_DELTA",
          delta: [{ op: "replace", path: "/status", value: status }],
        }),
      );
      if (status === "completed") {
        subscriber.next(event({ type: "RUN_FINISHED", runId, threadId }));
      } else if (status === "interrupted" || status === "cancelled") {
        subscriber.next(
          event({
            type: "RUN_FINISHED",
            runId,
            threadId,
            result: { status: "interrupted" },
          }),
        );
      } else {
        const error = asObject(turn.error);
        subscriber.next(
          event({
            type: "RUN_ERROR",
            code: "RUNTIME_ERROR",
            message: String(error.message ?? `turn ${status}`),
          }),
        );
      }
      onSettled();
      return;
    }
    subscriber.next(event({ type: "RAW", event: notification }));
  }
}
