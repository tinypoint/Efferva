export type JsonRpcId = number | string;

export type CodexNotification<TParams = unknown> = {
  method: string;
  params?: TParams;
};

export type CodexServerRequest<TParams = unknown> = CodexNotification<TParams> & {
  id: JsonRpcId;
};

export type CodexServerRequestHandler = (
  request: CodexServerRequest,
) => unknown | Promise<unknown>;

export type CodexClientOptions = {
  url: string | URL;
  clientInfo?: {
    name: string;
    title?: string;
    version: string;
  };
  capabilities?: {
    experimentalApi?: boolean;
    requestAttestation?: boolean;
    optOutNotificationMethods?: string[];
  };
  createWebSocket?: (url: string) => WebSocket;
  serverRequestHandler?: CodexServerRequestHandler;
};

export type CodexInitializeResult = {
  userAgent?: string;
  codexHome?: string;
  platformFamily?: string;
  platformOs?: string;
  [key: string]: unknown;
};

type PendingRequest = {
  method: string;
  resolve: (result: unknown) => void;
  reject: (error: Error) => void;
};

type JsonRpcResponse = {
  id: JsonRpcId;
  result?: unknown;
  error?: {
    code?: number;
    message?: string;
    data?: unknown;
  };
};

export class CodexRpcError extends Error {
  readonly method: string;
  readonly code?: number;
  readonly data?: unknown;

  constructor(
    method: string,
    error: { code?: number; message?: string; data?: unknown },
  ) {
    super(`${method}: ${error.message ?? "Codex RPC failed"}`);
    this.name = "CodexRpcError";
    this.method = method;
    this.code = error.code;
    this.data = error.data;
  }
}

export class CodexClient {
  readonly url: string;

  private readonly clientInfo: NonNullable<CodexClientOptions["clientInfo"]>;
  private readonly capabilities: NonNullable<
    CodexClientOptions["capabilities"]
  >;
  private readonly createWebSocket: (url: string) => WebSocket;
  private readonly notificationHandlers = new Set<
    (notification: CodexNotification) => void
  >();
  private readonly closeHandlers = new Set<(event: CloseEvent) => void>();
  private readonly pending = new Map<JsonRpcId, PendingRequest>();
  private serverRequestHandler?: CodexServerRequestHandler;
  private socket?: WebSocket;
  private connecting?: Promise<CodexInitializeResult>;
  private initialized?: CodexInitializeResult;
  private nextId = 1;
  private messageQueue: Promise<void> = Promise.resolve();

  constructor(options: CodexClientOptions) {
    this.url = String(options.url);
    this.clientInfo = options.clientInfo ?? {
      name: "efferva-web",
      title: "Efferva Web",
      version: "0.1.0",
    };
    this.capabilities = options.capabilities ?? {
      experimentalApi: true,
      requestAttestation: false,
    };
    this.createWebSocket =
      options.createWebSocket ?? ((url) => new WebSocket(url));
    this.serverRequestHandler = options.serverRequestHandler;
  }

  get isConnected(): boolean {
    return this.socket?.readyState === WebSocket.OPEN && Boolean(this.initialized);
  }

  get serverInfo(): CodexInitializeResult | undefined {
    return this.initialized;
  }

  async connect(): Promise<CodexInitializeResult> {
    if (this.isConnected && this.initialized) return this.initialized;
    if (this.connecting) return this.connecting;
    this.connecting = this.openAndInitialize();
    try {
      return await this.connecting;
    } finally {
      this.connecting = undefined;
    }
  }

  async reconnect(): Promise<CodexInitializeResult> {
    this.close();
    return this.connect();
  }

  async request<TResult = unknown, TParams = unknown>(
    method: string,
    params?: TParams,
  ): Promise<TResult> {
    await this.connect();
    return this.sendRequest<TResult, TParams>(method, params);
  }

  async notify<TParams = unknown>(
    method: string,
    params?: TParams,
  ): Promise<void> {
    await this.connect();
    this.send({ method, ...(params === undefined ? {} : { params }) });
  }

  onNotification(
    handler: (notification: CodexNotification) => void,
  ): () => void {
    this.notificationHandlers.add(handler);
    return () => this.notificationHandlers.delete(handler);
  }

  onClose(handler: (event: CloseEvent) => void): () => void {
    this.closeHandlers.add(handler);
    return () => this.closeHandlers.delete(handler);
  }

  setServerRequestHandler(handler?: CodexServerRequestHandler): void {
    this.serverRequestHandler = handler;
  }

  close(code = 1000, reason = "client closed"): void {
    const socket = this.socket;
    this.socket = undefined;
    this.initialized = undefined;
    if (
      socket &&
      (socket.readyState === WebSocket.OPEN ||
        socket.readyState === WebSocket.CONNECTING)
    ) {
      socket.close(code, reason);
    }
    this.rejectPending(new Error(reason));
  }

  private async openAndInitialize(): Promise<CodexInitializeResult> {
    const socket = this.createWebSocket(this.url);
    this.socket = socket;
    this.initialized = undefined;
    socket.addEventListener("message", (event) => {
      this.messageQueue = this.messageQueue
        .then(() => this.handleMessage(event.data))
        .catch(() => {
          socket.close(4002, "invalid Codex message");
        });
    });
    socket.addEventListener("close", (event) => this.handleClose(socket, event));

    await new Promise<void>((resolve, reject) => {
      const opened = () => {
        cleanup();
        resolve();
      };
      const failed = () => {
        cleanup();
        reject(new Error(`Unable to connect to Codex at ${this.url}`));
      };
      const closed = (event: CloseEvent) => {
        cleanup();
        reject(
          new Error(
            event.reason ||
              `Codex WebSocket closed before opening with code ${event.code}`,
          ),
        );
      };
      const cleanup = () => {
        socket.removeEventListener("open", opened);
        socket.removeEventListener("error", failed);
        socket.removeEventListener("close", closed);
      };
      socket.addEventListener("open", opened, { once: true });
      socket.addEventListener("error", failed, { once: true });
      socket.addEventListener("close", closed, { once: true });
    });

    try {
      const result = await this.sendRequest<CodexInitializeResult>(
        "initialize",
        {
          clientInfo: this.clientInfo,
          capabilities: this.capabilities,
        },
      );
      this.send({ method: "initialized", params: {} });
      this.initialized = result;
      return result;
    } catch (error) {
      socket.close(4011, "Codex initialization failed");
      throw error;
    }
  }

  private sendRequest<TResult, TParams = unknown>(
    method: string,
    params?: TParams,
  ): Promise<TResult> {
    const id = this.nextId++;
    return new Promise<TResult>((resolve, reject) => {
      this.pending.set(id, {
        method,
        resolve: (result) => resolve(result as TResult),
        reject,
      });
      try {
        this.send({
          id,
          method,
          ...(params === undefined ? {} : { params }),
        });
      } catch (error) {
        this.pending.delete(id);
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  private send(message: object): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error("Codex WebSocket is not open");
    }
    this.socket.send(JSON.stringify(message));
  }

  private async handleMessage(data: unknown): Promise<void> {
    let text: string;
    if (typeof data === "string") {
      text = data;
    } else if (data instanceof Blob) {
      text = await data.text();
    } else if (data instanceof ArrayBuffer) {
      text = new TextDecoder().decode(data);
    } else {
      throw new Error("Unsupported Codex WebSocket frame");
    }

    const message = JSON.parse(text) as
      | JsonRpcResponse
      | CodexNotification
      | CodexServerRequest;
    if ("method" in message) {
      if ("id" in message) {
        await this.handleServerRequest(message);
      } else {
        for (const handler of this.notificationHandlers) handler(message);
      }
      return;
    }

    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    if (message.error) {
      pending.reject(new CodexRpcError(pending.method, message.error));
    } else {
      pending.resolve(message.result);
    }
  }

  private async handleServerRequest(request: CodexServerRequest): Promise<void> {
    if (!this.serverRequestHandler) {
      this.send({
        id: request.id,
        error: {
          code: -32601,
          message: `No handler is registered for ${request.method}`,
        },
      });
      return;
    }
    try {
      const result = await this.serverRequestHandler(request);
      this.send({ id: request.id, result: result ?? null });
    } catch (error) {
      this.send({
        id: request.id,
        error: {
          code: -32000,
          message: error instanceof Error ? error.message : String(error),
        },
      });
    }
  }

  private handleClose(socket: WebSocket, event: CloseEvent): void {
    if (this.socket !== socket) return;
    this.socket = undefined;
    this.initialized = undefined;
    this.rejectPending(
      new Error(event.reason || `Codex WebSocket closed with code ${event.code}`),
    );
    for (const handler of this.closeHandlers) handler(event);
  }

  private rejectPending(error: Error): void {
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }
}
