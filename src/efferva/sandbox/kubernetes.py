from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from efferva.config import Settings
from efferva.sandbox.runtime import (
    BufferedSandboxRuntime,
    ProcessTransport,
    TransportEvent,
    TransportExited,
    TransportOutput,
)
from efferva.sandbox.types import (
    ProcessHandle,
    ProcessSpec,
    SandboxCapabilities,
    SandboxContext,
    SandboxHandle,
    WorkspaceHandle,
)

_PROVIDER_LABEL = "efferva-provider-contract"
_PROVIDER_VERSION = "v1"


class _KubernetesExecTransport(ProcessTransport):
    def __init__(
        self,
        *,
        websocket: Any,
        context_manager: Any,
        api_client: Any,
        pod: str,
        pid_file: str,
        tty: bool,
        terminate_callback: Any,
    ) -> None:
        self._websocket = websocket
        self._context_manager = context_manager
        self._api_client = api_client
        self._pod = pod
        self._pid_file = pid_file
        self._tty = tty
        self._terminate_callback = terminate_callback
        self._closed = False

    async def events(self) -> AsyncIterator[TransportEvent]:
        from aiohttp import WSMsgType
        from kubernetes_asyncio.stream.ws_client import WsApiClient

        exit_code: int | None = None
        async for message in self._websocket:
            if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING}:
                break
            if message.type == WSMsgType.ERROR:
                raise RuntimeError(str(self._websocket.exception()))
            if message.type != WSMsgType.BINARY:
                continue
            data = bytes(message.data)
            if not data:
                continue
            channel, payload = data[0], data[1:]
            if channel == 1 and payload:
                yield TransportOutput("pty" if self._tty else "stdout", payload)
            elif channel == 2 and payload:
                yield TransportOutput("stderr", payload)
            elif channel == 3 and payload:
                try:
                    exit_code = WsApiClient.parse_error_data(payload.decode())
                except (KeyError, ValueError, json.JSONDecodeError):
                    raise RuntimeError(payload.decode(errors="replace")) from None
        yield TransportExited(0 if exit_code is None else exit_code)

    async def write(self, data: bytes) -> None:
        if self._websocket.closed:
            raise BrokenPipeError("Kubernetes Exec stdin is closed")
        await self._websocket.send_bytes(bytes([0]) + data)

    async def resize(self, cols: int, rows: int) -> None:
        if not self._tty:
            raise RuntimeError("process has no PTY")
        if cols <= 0 or rows <= 0:
            raise ValueError("PTY size must be positive")
        payload = json.dumps({"Width": cols, "Height": rows}).encode()
        await self._websocket.send_bytes(bytes([4]) + payload)

    async def terminate(self) -> None:
        await self._terminate_callback(self._pod, self._pid_file)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._context_manager.__aexit__(None, None, None)
        with contextlib.suppress(Exception):
            await self._api_client.close()


class KubernetesSandboxRuntime(BufferedSandboxRuntime):
    def __init__(
        self,
        *,
        pod: str,
        namespace: str,
        workspace_path: str,
        configuration: Any,
    ) -> None:
        super().__init__(workspace_path)
        self.pod = pod
        self.namespace = namespace
        self._configuration = configuration

    async def _launch(
        self,
        spec: ProcessSpec,
        handle: ProcessHandle,
    ) -> ProcessTransport:
        from kubernetes_asyncio import client
        from kubernetes_asyncio.stream import WsApiClient

        pid_file = f"/tmp/efferva-{handle.id}.pid"
        wrapper = (
            'umask 077; pid_file="$1"; shift; '
            'cd "$1"; shift; '
            'for assignment in "$@"; do '
            'case "$assignment" in *=*) export "$assignment"; shift;; *) break;; esac; done; '
            'exec 3<&0; setsid "$@" <&3 3<&- & child="$!"; '
            'exec 3<&-; echo "$child" > "$pid_file"; wait "$child"'
        )
        assignments = [f"{key}={value}" for key, value in spec.env.items()]
        remote_command = [
            "/bin/sh",
            "-c",
            wrapper,
            "efferva-process",
            pid_file,
            spec.cwd,
            *assignments,
            *spec.argv,
        ]
        api_client = WsApiClient(configuration=self._configuration, heartbeat=30)
        core = client.CoreV1Api(api_client=api_client)
        context_manager = await core.connect_get_namespaced_pod_exec(
            self.pod,
            self.namespace,
            command=remote_command,
            container="sandbox",
            stdin=spec.pipe_stdin or spec.tty or spec.initial_stdin is not None,
            stdout=True,
            stderr=not spec.tty,
            tty=spec.tty,
            _preload_content=False,
        )
        websocket = await context_manager.__aenter__()
        return _KubernetesExecTransport(
            websocket=websocket,
            context_manager=context_manager,
            api_client=api_client,
            pod=self.pod,
            pid_file=pid_file,
            tty=spec.tty,
            terminate_callback=self._terminate_remote_process,
        )

    async def _terminate_remote_process(self, pod: str, pid_file: str) -> None:
        script = (
            'if [ -r "$1" ]; then pid="$(cat "$1")"; '
            '/bin/kill -TERM -- "-$pid" 2>/dev/null || true; sleep 0.2; '
            '/bin/kill -KILL -- "-$pid" 2>/dev/null || true; fi'
        )
        await self._control_exec(
            pod,
            ["/bin/sh", "-c", script, "efferva-kill", pid_file],
        )

    async def _control_exec(self, pod: str, argv: list[str]) -> None:
        from kubernetes_asyncio import client
        from kubernetes_asyncio.stream import WsApiClient

        api_client = WsApiClient(configuration=self._configuration, heartbeat=30)
        try:
            core = client.CoreV1Api(api_client=api_client)
            await core.connect_get_namespaced_pod_exec(
                pod,
                self.namespace,
                command=argv,
                container="sandbox",
                stdin=False,
                stdout=True,
                stderr=True,
                tty=False,
            )
        finally:
            await api_client.close()


class KubernetesSandboxProvider:
    name = "kubernetes"
    capabilities = SandboxCapabilities(
        streaming_exec=True,
        interactive_pty=True,
        persistent_workspace=True,
        snapshots=False,
        suspend_resume=True,
        port_forwarding=False,
        network_policy=False,
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._configured = False
        self._core: Any = None
        self._configuration: Any = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._runtimes: dict[str, KubernetesSandboxRuntime] = {}

    async def ensure_workspace(self, context: SandboxContext) -> WorkspaceHandle:
        await self._configure()
        name = self._resource_name(context)
        await self._create_pvc(name)
        return WorkspaceHandle(
            provider=self.name,
            external_ref=name,
            state={"mountPath": context.workspace_path},
        )

    async def start(
        self,
        context: SandboxContext,
        workspace: WorkspaceHandle,
    ) -> SandboxHandle:
        await self._configure()
        name = self._resource_name(context)
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            await self._replace_legacy_pod(name)
            await self._create_pod(name, context, workspace)
            await self._wait_ready(name)
        return SandboxHandle(
            provider=self.name,
            external_ref=name,
            workspace_id=context.workspace_id,
            state={"workspacePath": context.workspace_path},
        )

    async def connect(self, sandbox: SandboxHandle) -> KubernetesSandboxRuntime:
        await self._configure()
        runtime = self._runtimes.get(sandbox.external_ref)
        if runtime is None:
            runtime = KubernetesSandboxRuntime(
                pod=sandbox.external_ref,
                namespace=self._settings.kubernetes_namespace,
                workspace_path=str(
                    sandbox.state.get("workspacePath", self._settings.workspace_path)
                ),
                configuration=self._configuration,
            )
            self._runtimes[sandbox.external_ref] = runtime
        return runtime

    async def stop(self, sandbox: SandboxHandle) -> None:
        await self._configure()
        runtime = self._runtimes.pop(sandbox.external_ref, None)
        if runtime is not None:
            await runtime.close()
        await self._delete_pod(sandbox.external_ref)

    async def destroy(self, sandbox: SandboxHandle) -> None:
        await self.stop(sandbox)

    async def destroy_workspace(self, workspace: WorkspaceHandle) -> None:
        await self._configure()
        from kubernetes_asyncio.client import ApiException

        try:
            await self._core.delete_namespaced_persistent_volume_claim(
                workspace.external_ref,
                self._settings.kubernetes_namespace,
            )
        except ApiException as error:
            if error.status != 404:
                raise

    async def close(self) -> None:
        for runtime in tuple(self._runtimes.values()):
            await runtime.close()
        self._runtimes.clear()
        if self._core is not None:
            with contextlib.suppress(Exception):
                await self._core.api_client.close()

    async def _configure(self) -> None:
        if self._configured:
            return
        from kubernetes_asyncio import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            await config.load_kube_config()
        self._configuration = client.Configuration.get_default_copy()
        self._core = client.CoreV1Api(
            api_client=client.ApiClient(configuration=self._configuration)
        )
        self._configured = True

    @staticmethod
    def _resource_name(context: SandboxContext) -> str:
        return f"af-sandbox-{context.session_id.hex[:20]}"

    async def _create_pvc(self, name: str) -> None:
        from kubernetes_asyncio import client

        body = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=name, labels={"app": "efferva-sandbox"}),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                resources=client.V1VolumeResourceRequirements(
                    requests={"storage": self._settings.kubernetes_workspace_size}
                ),
                storage_class_name=self._settings.kubernetes_storage_class,
            ),
        )
        await self._create_or_ignore(
            self._core.create_namespaced_persistent_volume_claim,
            body,
        )

    async def _replace_legacy_pod(self, name: str) -> None:
        from kubernetes_asyncio.client import ApiException

        try:
            pod = await self._core.read_namespaced_pod(
                name,
                self._settings.kubernetes_namespace,
            )
        except ApiException as error:
            if error.status == 404:
                return
            raise
        labels = pod.metadata.labels or {}
        containers = {container.name for container in pod.spec.containers}
        if labels.get(_PROVIDER_LABEL) == _PROVIDER_VERSION and containers == {"sandbox"}:
            return
        await self._delete_pod(name)

    async def _create_pod(
        self,
        name: str,
        context: SandboxContext,
        workspace: WorkspaceHandle,
    ) -> None:
        from kubernetes_asyncio import client

        body = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=name,
                labels={
                    "app": "efferva-sandbox",
                    "efferva-session": str(context.session_id),
                    _PROVIDER_LABEL: _PROVIDER_VERSION,
                },
            ),
            spec=client.V1PodSpec(
                automount_service_account_token=False,
                restart_policy="Always",
                containers=[
                    client.V1Container(
                        name="sandbox",
                        image=self._settings.sandbox_image,
                        image_pull_policy="IfNotPresent",
                        command=["sleep", "infinity"],
                        security_context=client.V1SecurityContext(
                            allow_privilege_escalation=False,
                            capabilities=client.V1Capabilities(drop=["ALL"]),
                            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                        ),
                        resources=client.V1ResourceRequirements(
                            limits={
                                "cpu": self._settings.sandbox_cpu_limit,
                                "memory": self._settings.kubernetes_sandbox_memory_limit,
                            },
                            requests={"cpu": "100m", "memory": "128Mi"},
                        ),
                        volume_mounts=[
                            client.V1VolumeMount(
                                name="workspace",
                                mount_path=context.workspace_path,
                            )
                        ],
                    )
                ],
                volumes=[
                    client.V1Volume(
                        name="workspace",
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=workspace.external_ref
                        ),
                    )
                ],
            ),
        )
        await self._create_or_ignore(self._core.create_namespaced_pod, body)

    async def _create_or_ignore(self, operation: Any, body: Any) -> None:
        from kubernetes_asyncio.client import ApiException

        try:
            await operation(self._settings.kubernetes_namespace, body)
        except ApiException as error:
            if error.status != 409:
                raise

    async def _delete_pod(self, name: str) -> None:
        from kubernetes_asyncio.client import ApiException

        try:
            await self._core.delete_namespaced_pod(
                name,
                self._settings.kubernetes_namespace,
                grace_period_seconds=0,
            )
        except ApiException as error:
            if error.status != 404:
                raise
        async with asyncio.timeout(60):
            while True:
                try:
                    await self._core.read_namespaced_pod(
                        name,
                        self._settings.kubernetes_namespace,
                    )
                except ApiException as error:
                    if error.status == 404:
                        return
                    raise
                await asyncio.sleep(0.2)

    async def _wait_ready(self, name: str) -> None:
        async with asyncio.timeout(90):
            while True:
                pod = await self._core.read_namespaced_pod(
                    name,
                    self._settings.kubernetes_namespace,
                )
                conditions = pod.status.conditions or []
                if any(
                    condition.type == "Ready" and condition.status == "True"
                    for condition in conditions
                ):
                    return
                await asyncio.sleep(0.5)
