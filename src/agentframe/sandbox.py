from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agentframe.config import Settings
from agentframe.repository import SystemRepository


@dataclass(frozen=True)
class Sandbox:
    sandbox_id: str
    environment_id: str
    endpoint: str
    workspace_path: str


class SandboxBackend(Protocol):
    async def ensure(self, session_id: UUID, workspace_ref: str) -> Sandbox: ...

    async def close(self) -> None: ...


async def _command(*argv: str, check: bool = True) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()
    stdout = stdout_bytes.decode(errors="replace").strip()
    stderr = stderr_bytes.decode(errors="replace").strip()
    if check and process.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed ({process.returncode}): {stderr}")
    return process.returncode or 0, stdout, stderr


class DockerSandboxBackend:
    def __init__(self, settings: Settings, repository: SystemRepository) -> None:
        self._settings = settings
        self._repository = repository
        self._lock = asyncio.Lock()

    async def ensure(self, session_id: UUID, workspace_ref: str) -> Sandbox:
        container = f"af-sandbox-{session_id.hex}"
        volume = f"af-workspace-{session_id.hex}"
        endpoint = f"ws://{container}:{self._settings.sandbox_port}"
        async with self._lock:
            await self._ensure_network()
            await _command("docker", "volume", "create", volume)
            exists, _, _ = await _command("docker", "inspect", container, check=False)
            if exists != 0:
                await _command(
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    container,
                    "--restart",
                    "unless-stopped",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--cpus",
                    self._settings.sandbox_cpu_limit,
                    "--memory",
                    self._settings.docker_sandbox_memory_limit,
                    "--pids-limit",
                    str(self._settings.sandbox_pids_limit),
                    "--network",
                    self._settings.docker_network,
                    "--volume",
                    f"{volume}:{self._settings.workspace_path}",
                    "--label",
                    f"agentframe.session={session_id}",
                    self._settings.sandbox_image,
                    "--listen",
                    f"ws://0.0.0.0:{self._settings.sandbox_port}",
                )
            else:
                _, running, _ = await _command(
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    container,
                )
                if running != "true":
                    await _command("docker", "start", container)
            await self._wait_ready(container)
            await self._repository.upsert_sandbox_binding(
                session_id,
                backend="docker",
                sandbox_id=container,
                endpoint=endpoint,
                workspace_ref=workspace_ref,
                status="ready",
            )
        return Sandbox(
            sandbox_id=container,
            environment_id=str(session_id),
            endpoint=endpoint,
            workspace_path=self._settings.workspace_path,
        )

    async def _wait_ready(self, container: str) -> None:
        ready_url = f"http://127.0.0.1:{self._settings.sandbox_port}/readyz"
        async with asyncio.timeout(30):
            while True:
                status, _, _ = await _command(
                    "docker",
                    "exec",
                    container,
                    "curl",
                    "--fail",
                    "--silent",
                    ready_url,
                    check=False,
                )
                if status == 0:
                    return
                await asyncio.sleep(0.1)

    async def _ensure_network(self) -> None:
        exists, _, _ = await _command(
            "docker",
            "network",
            "inspect",
            self._settings.docker_network,
            check=False,
        )
        if exists != 0:
            await _command("docker", "network", "create", self._settings.docker_network)

    async def close(self) -> None:
        return None


class KubernetesSandboxBackend:
    def __init__(self, settings: Settings, repository: SystemRepository) -> None:
        self._settings = settings
        self._repository = repository
        self._configured = False
        self._core = None
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def ensure(self, session_id: UUID, workspace_ref: str) -> Sandbox:
        await self._configure()
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            name = f"af-sandbox-{session_id.hex[:20]}"
            await self._create_pvc(name)
            await self._create_service(name)
            await self._create_pod(name, session_id)
            await self._wait_ready(name)
            await self._wait_service_endpoint(name)
            endpoint = (
                f"ws://{name}.{self._settings.kubernetes_namespace}.svc.cluster.local:"
                f"{self._settings.sandbox_port}"
            )
            await self._repository.upsert_sandbox_binding(
                session_id,
                backend="kubernetes",
                sandbox_id=name,
                endpoint=endpoint,
                workspace_ref=workspace_ref,
                status="ready",
            )
            return Sandbox(
                sandbox_id=name,
                environment_id=str(session_id),
                endpoint=endpoint,
                workspace_path=self._settings.workspace_path,
            )

    async def _configure(self) -> None:
        if self._configured:
            return
        from kubernetes_asyncio import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            await config.load_kube_config()
        self._core = client.CoreV1Api()
        self._configured = True

    async def _create_pvc(self, name: str) -> None:
        from kubernetes_asyncio import client

        spec = client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1VolumeResourceRequirements(
                requests={"storage": self._settings.kubernetes_workspace_size}
            ),
            storage_class_name=self._settings.kubernetes_storage_class,
        )
        body = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=name, labels={"app": "agentframe-sandbox"}),
            spec=spec,
        )
        await self._create_or_ignore(
            self._core.create_namespaced_persistent_volume_claim,
            body,
        )

    async def _create_service(self, name: str) -> None:
        from kubernetes_asyncio import client

        body = client.V1Service(
            metadata=client.V1ObjectMeta(name=name, labels={"app": "agentframe-sandbox"}),
            spec=client.V1ServiceSpec(
                selector={"agentframe-sandbox": name},
                ports=[
                    client.V1ServicePort(
                        name="exec",
                        port=self._settings.sandbox_port,
                        target_port=self._settings.sandbox_port,
                    )
                ],
            ),
        )
        await self._create_or_ignore(self._core.create_namespaced_service, body)

    async def _create_pod(self, name: str, session_id: UUID) -> None:
        from kubernetes_asyncio import client

        body = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=name,
                labels={
                    "app": "agentframe-sandbox",
                    "agentframe-sandbox": name,
                    "agentframe-session": str(session_id),
                },
            ),
            spec=client.V1PodSpec(
                automount_service_account_token=False,
                restart_policy="Always",
                containers=[
                    client.V1Container(
                        name="exec-server",
                        image=self._settings.sandbox_image,
                        image_pull_policy="IfNotPresent",
                        args=[
                            "--listen",
                            f"ws://0.0.0.0:{self._settings.sandbox_port}",
                        ],
                        ports=[
                            client.V1ContainerPort(
                                name="exec",
                                container_port=self._settings.sandbox_port,
                            )
                        ],
                        readiness_probe=client.V1Probe(
                            http_get=client.V1HTTPGetAction(
                                path="/readyz",
                                port=self._settings.sandbox_port,
                            ),
                            period_seconds=1,
                            failure_threshold=30,
                        ),
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
                            requests={
                                "cpu": "100m",
                                "memory": "128Mi",
                            },
                        ),
                        volume_mounts=[
                            client.V1VolumeMount(
                                name="workspace",
                                mount_path=self._settings.workspace_path,
                            )
                        ],
                    )
                ],
                volumes=[
                    client.V1Volume(
                        name="workspace",
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=name
                        ),
                    )
                ],
            ),
        )
        await self._create_or_ignore(self._core.create_namespaced_pod, body)

    async def _create_or_ignore(self, operation, body) -> None:
        from kubernetes_asyncio.client import ApiException

        try:
            await operation(self._settings.kubernetes_namespace, body)
        except ApiException as error:
            if error.status != 409:
                raise

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

    async def _wait_service_endpoint(self, name: str) -> None:
        host = f"{name}.{self._settings.kubernetes_namespace}.svc.cluster.local"
        async with asyncio.timeout(30):
            while True:
                try:
                    _, writer = await asyncio.open_connection(
                        host,
                        self._settings.sandbox_port,
                    )
                except OSError:
                    await asyncio.sleep(0.1)
                    continue
                writer.close()
                await writer.wait_closed()
                return

    async def close(self) -> None:
        if self._core is not None:
            with contextlib.suppress(Exception):
                await self._core.api_client.close()


def create_sandbox_backend(
    settings: Settings,
    repository: SystemRepository,
) -> SandboxBackend:
    if settings.sandbox_backend == "docker":
        return DockerSandboxBackend(settings, repository)
    if settings.sandbox_backend in {"kubernetes", "kind"}:
        return KubernetesSandboxBackend(settings, repository)
    raise ValueError(f"unsupported sandbox backend: {settings.sandbox_backend}")
