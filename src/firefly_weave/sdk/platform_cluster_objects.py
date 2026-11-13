# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Every Kubernetes object a local cluster target needs, rendered from code.

One renderer writes the shared objects of a cluster (the weave-system namespace, the headless weave-api Service and
its EndpointSlice at the platform's address, the network policies and the private-origin entries) and the objects
of each target (its workers namespace, the runner's account and least-privilege rules, its configuration and
Deployment, and the worker Deployment at 0 replicas). Everything is labeled as managed by the platform, so a
server-side apply reverts a manual edit. Secrets are never rendered: their values reach the cluster only on stdin.
Pods run under the restricted Pod Security profile, mount no service account token except the runner's, read their
secrets only from owner-read-only copies, and reach the platform only when labeled as a runner or a worker.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from firefly_weave import private_origins
from firefly_weave.contracts.deployments import DeploymentCapability
from firefly_weave.deployment_runner.application import RunnerConfiguration
from firefly_weave.deployment_runner.policy import ComponentBinding, DestinationPolicy
from firefly_weave.sdk.platform_operations import cluster_clients

SYSTEM_NAMESPACE = "weave-system"
API_SERVICE = "weave-api"
API_HOST = f"{API_SERVICE}.{SYSTEM_NAMESPACE}.svc"
API_URL = f"http://{API_HOST}:8000"
KEYCLOAK_URL = f"http://{API_HOST}:8080"
# Clients sign in over loopback; the token relay forwards to Keycloak through the cluster network.
TOKEN_RELAY = f"{API_HOST}:8080"
RELAY_URL = "http://127.0.0.1:8080"
TOKEN_PATH = "/realms/weave/protocol/openid-connect/token"
MANAGED_BY = "weave-platform"
WORKER = "weave-worker"
WORKER_CONTAINER = "worker"
RUNNER_CONTAINER = "runner"
CONFIGURATION = "local"
RUNNER_CONTEXT = "weave-runner"
KUBECTL = "/usr/local/bin/kubectl"
CAPABILITIES: tuple[DeploymentCapability, ...] = ("observe", "update", "scale_workers", "restart_workers")
USER = 65532
# A worker's limits, which its runner's binding also allows at most, and the size the registered component states:
# the runner refuses a plan whose component asks for more than the binding allows.
WORKER_CPU_MILLIS = 1000
WORKER_MEMORY_MIB = 512
# The most workers a change request may scale the namespace's worker Deployment to; the runner refuses more.
MAX_WORKER_REPLICAS = 5
WORKER_GRACE_SECONDS = 240
RUNNER_GRACE_SECONDS = 30
HEARTBEAT_SECONDS = 90
IDENTITY_DIR = "/run/weave/identity"
SECRETS_DIR = "/run/weave/secrets"
ORIGINS_FILE = "/etc/weave/origins/private-origins.json"
WORKER_OAUTH_FILE = "/etc/weave/worker/oauth.json"
RUNNER_CONFIG_FILE = "/etc/weave/runner/runner.json"
RUNNER_OAUTH_FILE = "/etc/weave/runner/oauth.json"
KUBECONFIG_FILE = "/etc/weave/kubeconfig/config"
HEARTBEAT_FILE = "/tmp/weave-runner-heartbeat"
ROLE_LABEL = "firefly.weave/role"
TARGET_LABEL = "firefly.weave/target"
CONFIGURATION_ANNOTATION = "firefly.weave/configuration"
RESTARTED_AT = "firefly.weave/restarted-at"
IDENTITY_ROTATED = "firefly.weave/identity-rotated-at"
CONFIG_DIGEST = "firefly.weave/config-digest"
SECRET_KEY = "client-secret"
ORIGINS = "weave-private-origins"
WORKER_CONFIG = "weave-worker-config"
WORKER_IDENTITY = "weave-worker-identity"

# The adapter's kubectl calls, as rules: get and patch the worker Deployment, list, get and patch its pods (kubectl
# patch reads an object before it patches it). No watch, no Secrets or ConfigMaps, nothing in weave-system, no
# autoscalers (a cluster target has only a worker component).
RUNNER_RULES: tuple[dict[str, Any], ...] = (
    {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "patch"]},
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "patch"]},
)

# Copies each key of the mounted Secret (kubelet's links are skipped) to a memory-backed folder, owner read-only:
# kubelet gives a Secret volume 0440 with fsGroup, and the main container sees only these 0400 copies.
_COPY = """import os, sys
source, target = sys.argv[1], sys.argv[2]
for name in sorted(os.listdir(source)):
    if name.startswith('.'):
        continue
    with open(os.path.join(source, name), 'rb') as handle:
        data = handle.read()
    path = os.path.join(target, name)
    if os.path.lexists(path):
        os.unlink(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(data)
"""
# Exits non-zero when the runner's last contact with the platform is older than the limit, or never happened.
_ALIVE = "import os, sys, time; sys.exit(0 if time.time() - os.stat(sys.argv[1]).st_mtime < float(sys.argv[2]) else 1)"


@dataclass(frozen=True)
class ClusterNetwork:
    """A connected cluster: its Docker network and subnet, and where the platform's API and registry sit on it."""

    provider: str
    cluster: str
    network: str
    subnet: str
    api_address: str
    registry_address: str
    registry_port: int
    installation: str

    @property
    def name(self) -> str:
        return f"{self.provider}-{self.cluster}"


@dataclass(frozen=True)
class ClusterTarget:
    """One workers namespace of a cluster, registered as a deployment target with its own runner and identities."""

    network: ClusterNetwork
    namespace: str
    # None until connect registers the target: the namespace and the probes are rendered before that.
    target_id: UUID | None
    pair: int
    scope: dict[str, str]
    release_id: UUID
    worker_image: str
    runner_image: str
    namespace_uid: str
    identity_rotated_at: str

    @property
    def runner(self) -> str:
        return f"weave-runner-{self.namespace}"


def _labels(network: ClusterNetwork, target: ClusterTarget | None = None) -> dict[str, str]:
    labels = {
        "app.kubernetes.io/managed-by": MANAGED_BY,
        "firefly.weave/installation": network.installation,
        "firefly.weave/cluster": network.name,
    }
    if target is not None and target.target_id is not None:
        labels[TARGET_LABEL] = str(target.target_id)
    return labels


def _metadata(name: str, labels: dict[str, str], namespace: str | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name}
    if namespace is not None:
        metadata["namespace"] = namespace
    metadata["labels"] = labels
    return metadata


def _namespace(name: str, labels: dict[str, str]) -> dict[str, Any]:
    restricted = {
        f"pod-security.kubernetes.io/{mode}{suffix}": value
        for mode in ("enforce", "warn", "audit")
        for suffix, value in (("", "restricted"), ("-version", "latest"))
    }
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": _metadata(name, {**labels, **restricted})}


def _account(name: str, namespace: str, labels: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": _metadata(name, labels, namespace),
        "automountServiceAccountToken": False,
    }


def _policies(network: ClusterNetwork, namespace: str, labels: dict[str, str]) -> list[dict[str, Any]]:
    """Every pod may reach the cluster and the outside, but not the platform or the registry; Weave's runners and
    workers may reach the platform's API and Keycloak. Nodes pull through the registry, which no pod can reach. No
    pod accepts connections: none of Weave's pods serves anything."""
    api, registry = f"{network.api_address}/32", f"{network.registry_address}/32"
    egress = {
        "podSelector": {},
        "policyTypes": ["Egress"],
        "egress": [{"to": [{"namespaceSelector": {}}, {"ipBlock": {"cidr": "0.0.0.0/0", "except": [api, registry]}}]}],
    }
    access = {
        "podSelector": {"matchExpressions": [{"key": ROLE_LABEL, "operator": "In", "values": ["runner", "worker"]}]},
        "policyTypes": ["Egress"],
        "egress": [
            {
                "to": [{"ipBlock": {"cidr": api}}],
                "ports": [{"protocol": "TCP", "port": 8000}, {"protocol": "TCP", "port": 8080}],
            }
        ],
    }
    return [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": _metadata(name, labels, namespace),
            "spec": spec,
        }
        for name, spec in (
            ("weave-deny-ingress", {"podSelector": {}, "policyTypes": ["Ingress"]}),
            ("weave-egress", egress),
            ("weave-platform-access", access),
        )
    ]


def origin_entries(network: ClusterNetwork) -> list[private_origins.PrivateOrigin]:
    """The cluster's private-origin entries: the API and Keycloak over the cluster network, and the relay."""
    return subnet_entries(network.subnet)


def subnet_entries(cluster_subnet: str) -> list[private_origins.PrivateOrigin]:
    """The private-origin entries of a cluster on this Docker subnet; they depend on nothing else."""
    entry = private_origins.PrivateOrigin
    subnet = (cluster_subnet,)
    return [
        entry(origin=API_URL, purpose="platform-api", networks=subnet, credentials="bridge"),
        entry(origin=API_URL, purpose="runner", networks=subnet, credentials="bridge"),
        entry(origin=KEYCLOAK_URL, purpose="worker-auth", networks=subnet, credentials="bridge"),
        entry(origin=RELAY_URL, purpose="worker-auth", networks=("127.0.0.1/32",), credentials="loopback"),
    ]


def _origins_text(network: ClusterNetwork) -> str:
    policy = private_origins.PrivateOrigins(platform=private_origins.PLATFORM).with_entries(origin_entries(network))
    return private_origins.render(policy).decode()


def _config_map(name: str, namespace: str, labels: dict[str, str], data: dict[str, str]) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": _metadata(name, labels, namespace), "data": data}


def shared_objects(network: ClusterNetwork) -> list[dict[str, Any]]:
    """What every target of the cluster shares; the same bytes for each, so re-applying them changes nothing."""
    labels = _labels(network)
    ports = [{"name": "api", "port": 8000, "protocol": "TCP"}, {"name": "keycloak", "port": 8080, "protocol": "TCP"}]
    return [
        _namespace(SYSTEM_NAMESPACE, labels),
        _account("default", SYSTEM_NAMESPACE, labels),
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": _metadata(API_SERVICE, labels, SYSTEM_NAMESPACE),
            "spec": {"clusterIP": "None", "ports": [{**port, "targetPort": port["port"]} for port in ports]},
        },
        {
            "apiVersion": "discovery.k8s.io/v1",
            "kind": "EndpointSlice",
            "metadata": _metadata(
                "weave-api-platform",
                {
                    **labels,
                    "kubernetes.io/service-name": API_SERVICE,
                    "endpointslice.kubernetes.io/managed-by": MANAGED_BY,
                },
                SYSTEM_NAMESPACE,
            ),
            "addressType": "IPv4",
            "endpoints": [{"addresses": [network.api_address], "conditions": {"ready": True}}],
            "ports": ports,
        },
        *_policies(network, SYSTEM_NAMESPACE, labels),
        _config_map(ORIGINS, SYSTEM_NAMESPACE, labels, {"private-origins.json": _origins_text(network)}),
    ]


def destination_policy(target: ClusterTarget) -> DestinationPolicy:
    """What the target's runner may do: observe, update, scale and restart the one worker Deployment."""
    if target.target_id is None:
        raise ValueError("A runner's policy names its registered target")
    port = target.network.registry_port
    return DestinationPolicy(
        target_id=target.target_id,
        adapter="kubernetes",
        external_identity=target.namespace_uid,
        boundary=target.namespace,
        executable=KUBECTL,
        context=RUNNER_CONTEXT,
        capabilities=list(CAPABILITIES),
        components=[
            ComponentBinding(
                name=WORKER,
                kind="worker",
                configuration=CONFIGURATION,
                container=WORKER_CONTAINER,
                max_replicas=MAX_WORKER_REPLICAS,
                max_cpu_millis=WORKER_CPU_MILLIS,
                max_memory_mib=WORKER_MEMORY_MIB,
            )
        ],
        image_repositories=[f"127.0.0.1:{port}/example-sleep"],
    )


def _oauth(client: str) -> str:
    return json.dumps(
        {
            "token_endpoint": RELAY_URL + TOKEN_PATH,
            "client_id": client,
            "scope": "basic",
            "client_secret_file": f"{SECRETS_DIR}/{SECRET_KEY}",
        },
        indent=2,
    )


def _runner_configuration(target: ClusterTarget) -> str:
    """The runner's configuration, validated as the runner will read it: under the cluster's own origins."""
    policy = private_origins.PrivateOrigins(platform=private_origins.PLATFORM).with_entries(
        origin_entries(target.network)
    )
    with private_origins.installed(policy):
        configuration = RunnerConfiguration.model_validate(
            {
                "base_url": API_URL,
                "scope": target.scope,
                "destination": destination_policy(target),
                "oauth_file": RUNNER_OAUTH_FILE,
            }
        )
    return configuration.model_dump_json(indent=2, exclude_none=True)


def _kubeconfig(namespace: str) -> str:
    """kubectl's configuration in the runner pod: the API server through its projected, auto-rotated token."""
    account = "/var/run/secrets/kubernetes.io/serviceaccount"
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "in-cluster",
                    "cluster": {
                        "server": "https://kubernetes.default.svc",
                        "certificate-authority": f"{account}/ca.crt",
                    },
                }
            ],
            "users": [{"name": RUNNER_CONTEXT, "user": {"tokenFile": f"{account}/token"}}],
            "contexts": [
                {
                    "name": RUNNER_CONTEXT,
                    "context": {"cluster": "in-cluster", "user": RUNNER_CONTEXT, "namespace": namespace},
                }
            ],
            "current-context": RUNNER_CONTEXT,
        },
        indent=2,
    )


def _security() -> dict[str, Any]:
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}


def _pod_security() -> dict[str, Any]:
    return {
        "runAsNonRoot": True,
        "runAsUser": USER,
        "runAsGroup": USER,
        "fsGroup": USER,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def _resources(cpu: str, memory: str, cpu_limit: str, memory_limit: str) -> dict[str, Any]:
    return {"requests": {"cpu": cpu, "memory": memory}, "limits": {"cpu": cpu_limit, "memory": memory_limit}}


def _worker_resources() -> dict[str, Any]:
    # Also every probe's: a probe runs the worker's program, whose imports alone take about 95 MiB.
    return _resources("100m", "128Mi", f"{WORKER_CPU_MILLIS}m", f"{WORKER_MEMORY_MIB}Mi")


def _file(volume: str, path: str, key: str) -> dict[str, Any]:
    # A file of its own, not kubelet's link: private-origin files must be regular files.
    return {"name": volume, "mountPath": path, "subPath": key, "readOnly": True}


def _env(name: str, value: str) -> dict[str, Any]:
    return {"name": name, "value": value}


def _field(name: str, path: str) -> dict[str, Any]:
    # With the apiVersion the server stores, so a live worker compares equal to what was rendered.
    return {"name": name, "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": path}}}


def _identity_pod(image: str, secret: str, *, main: dict[str, Any], volumes: list[dict[str, Any]]) -> dict[str, Any]:
    """A pod spec whose main container reads its identity only from 0400 copies an init container makes."""
    init = {
        "name": "secrets",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-I", "-c", _COPY, IDENTITY_DIR, SECRETS_DIR],
        "resources": _resources("10m", "32Mi", "100m", "64Mi"),
        "securityContext": _security(),
        "volumeMounts": [
            {"name": "identity", "mountPath": IDENTITY_DIR, "readOnly": True},
            {"name": "secrets", "mountPath": SECRETS_DIR},
        ],
    }
    return {
        "initContainers": [init],
        "containers": [main],
        "volumes": [
            {"name": "identity", "secret": {"secretName": secret, "defaultMode": 0o440}},
            {"name": "secrets", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
            *volumes,
        ],
    }


def _worker_container(target: ClusterTarget, image: str, *, command: list[str] | None = None) -> dict[str, Any]:
    scope = target.scope
    container: dict[str, Any] = {"name": WORKER_CONTAINER, "image": image, "imagePullPolicy": "IfNotPresent"}
    if command is not None:
        container["command"] = command
    container |= {
        "env": [
            _env("WEAVE_API_URL", API_URL),
            _env(
                "WEAVE_ENVIRONMENT_URL",
                f"/api/v1/tenants/{scope['tenant_id']}/projects/{scope['project_id']}"
                f"/environments/{scope['environment_id']}",
            ),
            _env("WEAVE_WORKER_RELEASE_ID", str(target.release_id)),
            _env("WEAVE_WORKER_OAUTH_CONFIG_FILE", WORKER_OAUTH_FILE),
            _env("WEAVE_PRIVATE_ORIGINS_FILE", ORIGINS_FILE),
            _env("WEAVE_TOKEN_RELAY", TOKEN_RELAY),
            _field("WEAVE_POD_NAMESPACE", "metadata.namespace"),
            _field("WEAVE_NODE_NAME", "spec.nodeName"),
        ],
        "resources": _worker_resources(),
        "securityContext": _security(),
        "volumeMounts": [
            {"name": "secrets", "mountPath": SECRETS_DIR, "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},
            _file("origins", ORIGINS_FILE, "private-origins.json"),
            _file("config", WORKER_OAUTH_FILE, "oauth.json"),
        ],
    }
    return container


def _worker_volumes() -> list[dict[str, Any]]:
    return [
        {"name": "origins", "configMap": {"name": ORIGINS, "defaultMode": 0o444}},
        {"name": "config", "configMap": {"name": WORKER_CONFIG, "defaultMode": 0o444}},
    ]


def worker_deployment(target: ClusterTarget) -> dict[str, Any]:
    """The worker Deployment at 0 replicas. It is created once: its replicas, image, resources and restart time
    belong to reviewed change requests from then on."""
    labels = _labels(target.network, target)
    selector = {"app.kubernetes.io/name": WORKER}
    pod = _identity_pod(
        target.worker_image,
        WORKER_IDENTITY,
        main=_worker_container(target, target.worker_image),
        volumes=_worker_volumes(),
    )
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _metadata(WORKER, labels, target.namespace),
        "spec": {
            "replicas": 0,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {
                    "labels": {**labels, **selector, ROLE_LABEL: "worker"},
                    "annotations": {CONFIGURATION_ANNOTATION: CONFIGURATION},
                },
                "spec": {
                    "terminationGracePeriodSeconds": WORKER_GRACE_SECONDS,
                    "automountServiceAccountToken": False,
                    "securityContext": _pod_security(),
                    **pod,
                },
            },
        },
    }


def probe_pod(target: ClusterTarget, *, name: str, labeled: bool, command: list[str]) -> dict[str, Any]:
    """A short-lived pod from the worker image that checks reach: labeled, it is a worker in every way that matters
    to the network policies and sign-in; unlabeled, it mounts nothing and carries no role."""
    labels = {**_labels(target.network, target), "firefly.weave/probe": name}
    if labeled:
        spec = _identity_pod(
            target.worker_image,
            WORKER_IDENTITY,
            main=_worker_container(target, target.worker_image, command=command),
            volumes=_worker_volumes(),
        )
        labels[ROLE_LABEL] = "worker"
    else:
        spec = {
            "containers": [
                {
                    "name": "probe",
                    "image": target.worker_image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": command,
                    "resources": _worker_resources(),
                    "securityContext": _security(),
                    "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                }
            ],
            "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}],
        }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": _metadata(name, labels, target.namespace),
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": 60,
            "terminationGracePeriodSeconds": 1,
            "automountServiceAccountToken": False,
            "securityContext": _pod_security(),
            **spec,
        },
    }


def _runner_deployment(target: ClusterTarget, labels: dict[str, str], digest: str) -> dict[str, Any]:
    selector = {"app.kubernetes.io/name": "weave-runner", TARGET_LABEL: str(target.target_id)}
    main = {
        "name": RUNNER_CONTAINER,
        "image": target.runner_image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["weave", "operations", "runner", "run", "--config", RUNNER_CONFIG_FILE],
        "env": [
            _env("WEAVE_PRIVATE_ORIGINS_FILE", ORIGINS_FILE),
            _env("WEAVE_TOKEN_RELAY", TOKEN_RELAY),
            _env("WEAVE_RUNNER_HEARTBEAT_FILE", HEARTBEAT_FILE),
            _env("KUBECONFIG", KUBECONFIG_FILE),
            # kubectl keeps its discovery cache under HOME; the root filesystem is read-only.
            _env("HOME", "/tmp"),
        ],
        "resources": _resources("50m", "96Mi", "500m", "256Mi"),
        "securityContext": _security(),
        "livenessProbe": {
            "exec": {"command": ["python", "-I", "-c", _ALIVE, HEARTBEAT_FILE, str(HEARTBEAT_SECONDS)]},
            "initialDelaySeconds": 30,
            "periodSeconds": 30,
            "timeoutSeconds": 5,
            "failureThreshold": 3,
        },
        "volumeMounts": [
            {"name": "secrets", "mountPath": SECRETS_DIR, "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},
            _file("origins", ORIGINS_FILE, "private-origins.json"),
            _file("config", RUNNER_CONFIG_FILE, "runner.json"),
            _file("config", RUNNER_OAUTH_FILE, "oauth.json"),
            _file("kubeconfig", KUBECONFIG_FILE, "config"),
        ],
    }
    pod = _identity_pod(
        target.runner_image,
        f"weave-runner-identity-{target.namespace}",
        main=main,
        volumes=[
            {"name": "origins", "configMap": {"name": ORIGINS, "defaultMode": 0o444}},
            {"name": "config", "configMap": {"name": f"weave-runner-config-{target.namespace}", "defaultMode": 0o444}},
            {
                "name": "kubeconfig",
                "configMap": {"name": f"weave-runner-kubeconfig-{target.namespace}", "defaultMode": 0o444},
            },
        ],
    )
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _metadata(target.runner, labels, SYSTEM_NAMESPACE),
        "spec": {
            "replicas": 1,
            # One runner per target at a time; the platform's leases fence any overlap anyway.
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {
                    "labels": {**labels, **selector, ROLE_LABEL: "runner"},
                    # A new identity or configuration rolls the runner onto it with the next apply.
                    "annotations": {IDENTITY_ROTATED: target.identity_rotated_at, CONFIG_DIGEST: digest},
                },
                "spec": {
                    "serviceAccountName": target.runner,
                    "automountServiceAccountToken": True,
                    "terminationGracePeriodSeconds": RUNNER_GRACE_SECONDS,
                    "securityContext": _pod_security(),
                    **pod,
                },
            },
        },
    }


def namespace_objects(target: ClusterTarget) -> list[dict[str, Any]]:
    """The workers namespace with its network policies and private origins: what the probes need before the target
    is registered, and the start of the target's own objects after."""
    labels = _labels(target.network, target)
    namespace = target.namespace
    return [
        _namespace(namespace, labels),
        _account("default", namespace, labels),
        *_policies(target.network, namespace, labels),
        _config_map(ORIGINS, namespace, labels, {"private-origins.json": _origins_text(target.network)}),
    ]


def worker_config(target: ClusterTarget) -> dict[str, Any]:
    """The workers' sign-in settings for the target's pair, without the secret."""
    labels = _labels(target.network, target)
    oauth = {"oauth.json": _oauth(cluster_clients(target.pair)[1])}
    return _config_map(WORKER_CONFIG, target.namespace, labels, oauth)


def target_objects(target: ClusterTarget) -> list[dict[str, Any]]:
    """A target's own objects, in apply order; the worker Deployment is separate (created once)."""
    labels = _labels(target.network, target)
    namespace, runner = target.namespace, target.runner
    subject = [{"kind": "ServiceAccount", "name": runner, "namespace": SYSTEM_NAMESPACE}]
    runner_config = {
        "runner.json": _runner_configuration(target),
        "oauth.json": _oauth(cluster_clients(target.pair)[0]),
    }
    kubeconfig = {"config": _kubeconfig(namespace)}
    origins = {"private-origins.json": _origins_text(target.network)}
    digest = hashlib.sha256(json.dumps([runner_config, kubeconfig, origins], sort_keys=True).encode()).hexdigest()
    return [
        *namespace_objects(target),
        worker_config(target),
        _account(runner, SYSTEM_NAMESPACE, labels),
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": _metadata("weave-runner", labels, namespace),
            "rules": list(RUNNER_RULES),
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": _metadata("weave-runner", labels, namespace),
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "weave-runner"},
            "subjects": subject,
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": _metadata(runner, labels),
            "rules": [{"apiGroups": [""], "resources": ["namespaces"], "resourceNames": [namespace], "verbs": ["get"]}],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": _metadata(runner, labels),
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": runner},
            "subjects": subject,
        },
        _config_map(f"weave-runner-kubeconfig-{namespace}", SYSTEM_NAMESPACE, labels, kubeconfig),
        _config_map(f"weave-runner-config-{namespace}", SYSTEM_NAMESPACE, labels, runner_config),
        _runner_deployment(target, labels, digest),
    ]


_CONTAINER_HARDENING = ("securityContext", "env", "envFrom", "volumeMounts", "command", "args", "lifecycle")
# Only the command a probe runs: the server fills in a probe's timings.
_PROBES = ("livenessProbe", "readinessProbe", "startupProbe")
# The pod's isolation, identity and name resolution: hostAliases or dnsConfig could send the worker's sign-in to
# another host.
_POD_HARDENING = (
    "hostNetwork",
    "hostPID",
    "hostIPC",
    "shareProcessNamespace",
    "hostAliases",
    "dnsConfig",
    "runtimeClassName",
    "serviceAccountName",
)


def _hardening(spec: dict[str, Any]) -> dict[str, Any]:
    """The fields of a pod spec that keep it hardened, keyed by their dotted path."""
    fields: dict[str, Any] = {
        "spec.template.spec.automountServiceAccountToken": spec.get("automountServiceAccountToken"),
        "spec.template.spec.securityContext": spec.get("securityContext"),
    }
    for key in _POD_HARDENING:
        fields[f"spec.template.spec.{key}"] = spec.get(key)
    # The server stores ClusterFirst when none is given.
    fields["spec.template.spec.dnsPolicy"] = spec.get("dnsPolicy") or "ClusterFirst"
    for volume in spec.get("volumes") or []:
        # Each volume's kind of source, what it names, its storage medium and file mode; the server fills in neither
        # when it's set, so a live worker still compares equal, and the other defaults inside a source are left out.
        source = sorted(key for key in volume if key != "name")
        settings = volume.get(source[0]) if len(source) == 1 and isinstance(volume.get(source[0]), dict) else {}
        fields[f"spec.template.spec.volumes[{volume.get('name')}]"] = {
            "source": ",".join(source),
            "reference": settings.get("secretName") or settings.get("name"),
            "medium": settings.get("medium"),
            "defaultMode": settings.get("defaultMode"),
        }
    for group in ("initContainers", "containers"):
        for container in spec.get(group) or []:
            # What each container runs and everything it reads: an added envFrom or mount brings a Secret in without
            # touching the pod's volumes, and an exec hook or probe runs another command. Change requests never set
            # these, and the rendered worker has no hooks or probes for the server to fill in.
            fields[f"spec.template.spec.{group}[{container.get('name')}]"] = {
                **{key: container.get(key) for key in _CONTAINER_HARDENING},
                **{f"{probe}.exec": (container.get(probe) or {}).get("exec") for probe in _PROBES},
            }
    return fields


def _leaves(path: str, value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value:
        found: dict[str, Any] = {}
        for key in value:
            found |= _leaves(f"{path}.{key}", value[key])
        return found
    return {path: value}


def worker_drift(live: dict[str, Any], target: ClusterTarget) -> list[str]:
    """Dotted paths of the live worker Deployment's hardening fields that differ from the rendered ones.

    Replicas, image, resources and the restart time are left out: reviewed change requests own them.
    """
    expected = _hardening(worker_deployment(target)["spec"]["template"]["spec"])
    actual = _hardening(live.get("spec", {}).get("template", {}).get("spec", {}))
    drift: set[str] = set()
    for path in sorted(set(expected) | set(actual)):
        if path not in expected or path not in actual:
            # A container added or removed: the whole entry differs.
            if expected.get(path) != actual.get(path):
                drift.add(path)
            continue
        mine, theirs = _leaves(path, expected[path]), _leaves(path, actual[path])
        drift |= {leaf for leaf in set(mine) | set(theirs) if mine.get(leaf) != theirs.get(leaf)}
    return sorted(drift)
