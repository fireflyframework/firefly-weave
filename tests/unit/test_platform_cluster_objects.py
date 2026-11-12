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

"""Every object a local cluster target needs comes from one renderer: restricted pods, least privilege, no secrets."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

import yaml

from firefly_weave import private_origins as po
from firefly_weave.deployment_runner.application import RunnerConfiguration
from firefly_weave.sdk import platform_cluster_objects as objects

ROOT = Path(__file__).resolve().parents[2]
NETWORK = objects.ClusterNetwork(
    provider="kind",
    cluster="weave-test",
    network="weave-kind-weave-test",
    subnet="172.30.0.0/16",
    api_address="172.30.0.5",
    registry_address="172.30.0.6",
    registry_port=41234,
    installation="a1b2c3d4e5f6",
)


def target(namespace="weave-workers", pair=1):
    return objects.ClusterTarget(
        network=NETWORK,
        namespace=namespace,
        target_id=UUID("11111111-2222-3333-4444-555555555555"),
        pair=pair,
        scope={
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "project_id": "00000000-0000-0000-0000-000000000002",
            "environment_id": "00000000-0000-0000-0000-000000000003",
        },
        release_id=UUID("99999999-8888-7777-6666-555555555555"),
        worker_image="127.0.0.1:41234/example-sleep@sha256:" + "a" * 64,
        runner_image="127.0.0.1:41234/weave/runner@sha256:" + "b" * 64,
        namespace_uid="namespace-uid-1",
        identity_rotated_at="2026-11-12T10:00:00Z",
    )


def rendered():
    item = target()
    return [*objects.shared_objects(NETWORK), *objects.target_objects(item), objects.worker_deployment(item)]


def pods():
    """Every pod spec the renderer writes, with its name: the runner, the worker and both probes."""
    item = target()
    found = {
        doc["metadata"]["name"]: doc["spec"]["template"]["spec"] for doc in rendered() if doc["kind"] == "Deployment"
    }
    for labeled in (True, False):
        probe = objects.probe_pod(item, name=f"weave-probe-{labeled}", labeled=labeled, command=["python", "-c", "0"])
        found[probe["metadata"]["name"]] = probe["spec"]
    return found


def effective(pod, container, field):
    return container.get("securityContext", {}).get(field, pod.get("securityContext", {}).get(field))


def test_every_pod_passes_the_restricted_profile_fields():
    specs = pods()
    assert set(specs) == {"weave-runner-weave-workers", "weave-worker", "weave-probe-True", "weave-probe-False"}
    for name, pod in specs.items():
        assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}, name
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            assert not pod.get(key), (name, key)
        assert not any("hostPath" in volume for volume in pod.get("volumes", [])), name
        for container in [*pod.get("initContainers", []), *pod["containers"]]:
            label = (name, container["name"])
            assert effective(pod, container, "runAsNonRoot") is True, label
            assert effective(pod, container, "runAsUser") == 65532, label
            context = container["securityContext"]
            assert context["readOnlyRootFilesystem"] is True and context["allowPrivilegeEscalation"] is False, label
            assert context["capabilities"] == {"drop": ["ALL"]} and not context.get("privileged"), label
            assert set(container["resources"]) == {"requests", "limits"}, label
            assert {"cpu", "memory"} <= set(container["resources"]["requests"]), label


def test_both_namespaces_enforce_the_restricted_profile():
    namespaces = [doc for doc in rendered() if doc["kind"] == "Namespace"]
    assert {doc["metadata"]["name"] for doc in namespaces} == {"weave-system", "weave-workers"}
    for doc in namespaces:
        labels = doc["metadata"]["labels"]
        for mode in ("enforce", "warn", "audit"):
            assert labels[f"pod-security.kubernetes.io/{mode}"] == "restricted", (doc["metadata"]["name"], mode)


def test_only_the_runner_pod_mounts_a_service_account_token():
    specs = pods()
    assert specs["weave-runner-weave-workers"]["automountServiceAccountToken"] is True
    assert specs["weave-runner-weave-workers"]["serviceAccountName"] == "weave-runner-weave-workers"
    for name in ("weave-worker", "weave-probe-True", "weave-probe-False"):
        assert specs[name]["automountServiceAccountToken"] is False, name
    accounts = {
        (doc["metadata"]["namespace"], doc["metadata"]["name"]): doc
        for doc in rendered()
        if doc["kind"] == "ServiceAccount"
    }
    assert set(accounts) == {
        ("weave-system", "default"),
        ("weave-workers", "default"),
        ("weave-system", "weave-runner-weave-workers"),
    }
    assert all(doc["automountServiceAccountToken"] is False for doc in accounts.values())


def test_no_secret_is_an_environment_variable_or_a_rendered_object():
    text = json.dumps([*rendered(), *(objects.probe_pod(target(), name="p", labeled=True, command=["x"]),)])
    assert "secretKeyRef" not in text and "envFrom" not in text
    assert not [doc for doc in rendered() if doc["kind"] == "Secret"]


def test_the_main_containers_see_only_0400_copies():
    for name, pod in pods().items():
        secret_volumes = {volume["name"] for volume in pod.get("volumes", []) if "secret" in volume}
        if name == "weave-probe-False":
            assert secret_volumes == set(), "an unlabeled probe mounts nothing"
            continue
        assert secret_volumes == {"identity"}, name
        (init,) = pod["initContainers"]
        assert init["name"] == "secrets"
        assert {mount["name"] for mount in init["volumeMounts"]} == {"identity", "secrets"}
        for container in pod["containers"]:
            mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
            assert "identity" not in mounts, name
            assert mounts["secrets"]["mountPath"] == "/run/weave/secrets", name
        secrets = next(volume for volume in pod["volumes"] if volume["name"] == "secrets")
        assert secrets["emptyDir"] == {"medium": "Memory", "sizeLimit": "1Mi"}, name


def test_the_init_copy_writes_each_key_owner_read_only_and_skips_kubelet_entries(tmp_path):
    """Run the init container's own copy script against a Secret volume laid out the way kubelet mounts one."""
    source, destination = tmp_path / "identity", tmp_path / "secrets"
    (source / "..2026_11_12").mkdir(parents=True)
    destination.mkdir()
    (source / "..2026_11_12" / "client-secret").write_text("s3cret")
    (source / "..data").symlink_to("..2026_11_12")
    (source / "client-secret").symlink_to("..data/client-secret")
    (destination / "client-secret").write_text("stale")
    (destination / "client-secret").chmod(0o400)
    (init,) = pods()["weave-worker"]["initContainers"]
    command = init["command"]
    assert command[0] == "python" and command[-2:] == ["/run/weave/identity", "/run/weave/secrets"]
    subprocess.run([sys.executable, *command[1:-2], str(source), str(destination)], check=True, timeout=30)
    assert sorted(item.name for item in destination.iterdir()) == ["client-secret"]
    assert (destination / "client-secret").read_text() == "s3cret"
    assert (destination / "client-secret").stat().st_mode & 0o777 == 0o400


def test_the_runner_liveness_probe_fails_on_a_stale_or_missing_heartbeat(tmp_path):
    (probe,) = [
        container["livenessProbe"]
        for container in pods()["weave-runner-weave-workers"]["containers"]
        if container["name"] == "runner"
    ]
    assert (probe["periodSeconds"], probe["failureThreshold"]) == (30, 3)
    command = probe["exec"]["command"]
    assert command[-2:] == ["/tmp/weave-runner-heartbeat", "90"]
    beat = tmp_path / "heartbeat"

    def run():
        return subprocess.run([sys.executable, *command[1:-2], str(beat), "90"], timeout=30).returncode

    assert run() != 0
    beat.write_text("")
    assert run() == 0
    os.utime(beat, (time.time() - 91, time.time() - 91))
    assert run() != 0


def runner_argv():
    """The kubectl calls the Kubernetes adapter makes to observe, scale down and restart, on the tests' fake."""
    import asyncio

    import test_deployment_kubernetes as fake

    from firefly_weave.deployment_runner.kubernetes import KubernetesAdapter

    async def record():
        calls = []
        policy, plan, server = fake.scaled(3, 1)
        server.pods = fake.three_pods()
        await KubernetesAdapter(policy, command=server.command).observe()
        await KubernetesAdapter(policy, command=server.command).apply(plan, server.before_write)
        calls += server.calls
        policy, plan, server, clock = fake.restart()
        await KubernetesAdapter(policy, command=server.command, sleep=clock.sleep, clock=clock).apply(
            plan, server.before_write
        )
        return calls + server.calls

    return [argv[6:] for argv, _ in asyncio.run(record())]


def test_the_runner_rules_are_exactly_what_its_commands_need():
    """Map every kubectl call the adapter makes to its API resource and verb; the rendered RBAC must equal that."""
    needed = set()
    for argv in runner_argv():
        verb, (resource, _, group) = argv[0], argv[1].partition(".")
        # `get` with a selector lists; kubectl's short singular names the plural resource.
        if verb == "get" and any(item.startswith("--selector=") for item in argv):
            verb = "list"
        needed.add((group, {"namespace": "namespaces"}.get(resource, resource), verb))
    granted = {
        (group, resource, verb)
        for rule in objects.RUNNER_RULES
        for group in rule["apiGroups"]
        for resource in rule["resources"]
        for verb in rule["verbs"]
    }
    docs = {(doc["kind"], doc["metadata"]["name"]): doc for doc in objects.target_objects(target())}
    role = docs[("Role", "weave-runner")]
    cluster_role = docs[("ClusterRole", "weave-runner-weave-workers")]
    assert role["metadata"]["namespace"] == "weave-workers" and role["rules"] == list(objects.RUNNER_RULES)
    assert cluster_role["rules"] == [
        {"apiGroups": [""], "resources": ["namespaces"], "resourceNames": ["weave-workers"], "verbs": ["get"]}
    ]
    assert granted | {("", "namespaces", "get")} == needed
    for kind, name in (("RoleBinding", "weave-runner"), ("ClusterRoleBinding", "weave-runner-weave-workers")):
        assert docs[(kind, name)]["subjects"] == [
            {"kind": "ServiceAccount", "name": "weave-runner-weave-workers", "namespace": "weave-system"}
        ]


def policies(docs, namespace):
    return {
        doc["metadata"]["name"]: doc["spec"]
        for doc in docs
        if doc["kind"] == "NetworkPolicy" and doc["metadata"]["namespace"] == namespace
    }


def test_the_policies_open_the_platform_only_to_labeled_pods_and_the_registry_to_none():
    docs = rendered()
    for namespace in ("weave-system", "weave-workers"):
        found = policies(docs, namespace)
        assert found["weave-egress"] == {
            "podSelector": {},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {"namespaceSelector": {}},
                        {"ipBlock": {"cidr": "0.0.0.0/0", "except": ["172.30.0.5/32", "172.30.0.6/32"]}},
                    ]
                }
            ],
        }, namespace
        assert found["weave-platform-access"] == {
            "podSelector": {
                "matchExpressions": [{"key": "firefly.weave/role", "operator": "In", "values": ["runner", "worker"]}]
            },
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [{"ipBlock": {"cidr": "172.30.0.5/32"}}],
                    "ports": [{"protocol": "TCP", "port": 8000}, {"protocol": "TCP", "port": 8080}],
                }
            ],
        }, namespace
    labels = {
        name: spec["template"]["metadata"]["labels"].get("firefly.weave/role")
        for name, spec in ((doc["metadata"]["name"], doc["spec"]) for doc in docs if doc["kind"] == "Deployment")
    }
    assert labels == {"weave-runner-weave-workers": "runner", "weave-worker": "worker"}
    assert (
        "firefly.weave/role"
        not in objects.probe_pod(target(), name="p", labeled=False, command=["x"])["metadata"]["labels"]
    )


def test_the_origins_hold_bridge_entries_for_the_cluster_subnet_only():
    entries = objects.origin_entries(NETWORK)
    bridges = [entry for entry in entries if entry.credentials == "bridge"]
    assert {(entry.origin, entry.purpose) for entry in bridges} == {
        ("http://weave-api.weave-system.svc:8000", "platform-api"),
        ("http://weave-api.weave-system.svc:8000", "runner"),
        ("http://weave-api.weave-system.svc:8080", "worker-auth"),
    }
    assert all(entry.networks == ("172.30.0.0/16",) for entry in bridges)
    assert [(entry.origin, entry.purpose, entry.credentials) for entry in entries if entry not in bridges] == [
        ("http://127.0.0.1:8080", "worker-auth", "loopback")
    ]
    maps = [
        doc for doc in rendered() if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "weave-private-origins"
    ]
    assert {doc["metadata"]["namespace"] for doc in maps} == {"weave-system", "weave-workers"}
    for doc in maps:
        assert set(po.parse(doc["data"]["private-origins.json"].encode()).entries) == set(entries)


def test_the_runner_configuration_validates_with_the_cluster_origins():
    docs = {(doc["kind"], doc["metadata"]["name"]): doc for doc in objects.target_objects(target())}
    data = docs[("ConfigMap", "weave-runner-config-weave-workers")]["data"]
    with po.installed(po.PrivateOrigins(platform=po.PLATFORM).with_entries(objects.origin_entries(NETWORK))):
        config = RunnerConfiguration.model_validate_json(data["runner.json"])
    assert config.base_url == "http://weave-api.weave-system.svc:8000"
    assert config.oauth_file == "/etc/weave/runner/oauth.json"
    policy = config.destination
    assert policy == objects.destination_policy(target())
    assert (policy.adapter, policy.boundary, policy.external_identity, policy.context, policy.executable) == (
        "kubernetes",
        "weave-workers",
        "namespace-uid-1",
        "weave-runner",
        "/usr/local/bin/kubectl",
    )
    assert policy.capabilities == ["observe", "update", "scale_workers", "restart_workers"]
    (binding,) = policy.components
    assert (binding.name, binding.kind, binding.container, binding.configuration) == (
        "weave-worker",
        "worker",
        "worker",
        "local",
    )
    assert (binding.max_replicas, binding.max_cpu_millis, binding.max_memory_mib) == (5, 1000, 512)
    assert policy.image_repositories == ["127.0.0.1:41234/example-sleep"]
    oauth = json.loads(data["oauth.json"])
    assert oauth == {
        "token_endpoint": "http://127.0.0.1:8080/realms/weave/protocol/openid-connect/token",
        "client_id": "weave-cluster-runner-1",
        "scope": "basic",
        "client_secret_file": "/run/weave/secrets/client-secret",
    }


def test_shared_objects_render_the_same_for_every_target_of_a_cluster():
    other = target(namespace="weave-workers-b", pair=2)
    assert json.dumps(objects.shared_objects(other.network)) == json.dumps(objects.shared_objects(NETWORK))
    assert json.dumps(objects.target_objects(other)) != json.dumps(objects.target_objects(target()))


def test_a_worker_deployment_whose_security_context_was_edited_reports_that_field():
    live = objects.worker_deployment(target())
    assert objects.worker_drift(live, target()) == []
    edited = json.loads(json.dumps(live))
    edited["spec"]["template"]["spec"]["containers"][0]["securityContext"]["readOnlyRootFilesystem"] = False
    assert objects.worker_drift(edited, target()) == [
        "spec.template.spec.containers[worker].securityContext.readOnlyRootFilesystem"
    ]
    reviewed = json.loads(json.dumps(live))
    reviewed["spec"]["replicas"] = 3
    reviewed["spec"]["template"]["spec"]["containers"][0]["image"] = "127.0.0.1:41234/example-sleep@sha256:" + "c" * 64
    reviewed["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"cpu": "500m"}}
    reviewed["spec"]["template"]["metadata"]["annotations"]["firefly.weave/restarted-at"] = "2026-11-12T11:00:00Z"
    assert objects.worker_drift(reviewed, target()) == []
    widened = json.loads(json.dumps(live))
    widened["spec"]["template"]["spec"]["hostNetwork"] = True
    widened["spec"]["template"]["spec"]["containers"].append({"name": "sidecar", "image": "x"})
    assert objects.worker_drift(widened, target()) == [
        "spec.template.spec.containers[sidecar]",
        "spec.template.spec.hostNetwork",
    ]


def test_the_worker_template_matches_the_rendered_worker_hardening():
    template = yaml.safe_load((ROOT / "deploy/kubernetes/worker.yaml").read_text())
    rendered_pod = objects.worker_deployment(target())["spec"]["template"]
    pod = template["spec"]["template"]
    assert pod["spec"]["securityContext"] == rendered_pod["spec"]["securityContext"]
    assert pod["spec"]["containers"][0]["securityContext"] == rendered_pod["spec"]["containers"][0]["securityContext"]
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["metadata"]["annotations"]["firefly.weave/configuration"] == "__WEAVE_WORKER_CONFIGURATION__"
    env = {item["name"]: item for item in pod["spec"]["containers"][0]["env"]}
    assert env["WEAVE_POD_NAMESPACE"]["valueFrom"] == {"fieldRef": {"fieldPath": "metadata.namespace"}}
    assert env["WEAVE_NODE_NAME"]["valueFrom"] == {"fieldRef": {"fieldPath": "spec.nodeName"}}


def test_every_object_is_labeled_as_managed_by_the_platform():
    for doc in rendered():
        labels = doc["metadata"]["labels"]
        assert labels["app.kubernetes.io/managed-by"] == "weave-platform", doc["metadata"]["name"]
        assert labels["firefly.weave/installation"] == "a1b2c3d4e5f6"
        assert labels["firefly.weave/cluster"] == "kind-weave-test"
    for doc in objects.target_objects(target()):
        assert doc["metadata"]["labels"]["firefly.weave/target"] == "11111111-2222-3333-4444-555555555555"


def stored(document):
    """A Deployment the way the API server stores it: the defaults it fills in, beyond what was applied."""
    live = json.loads(json.dumps(document))
    pod = live["spec"]["template"]["spec"]
    pod |= {"dnsPolicy": "ClusterFirst", "restartPolicy": "Always", "schedulerName": "default-scheduler"}
    for container in [*pod.get("initContainers", []), *pod["containers"]]:
        container |= {"terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File"}
        for item in container.get("env", []):
            if "valueFrom" in item:
                item["valueFrom"]["fieldRef"].setdefault("apiVersion", "v1")
    return live


def test_a_live_worker_as_the_server_stores_it_reports_no_drift():
    assert objects.worker_drift(stored(objects.worker_deployment(target())), target()) == []


def test_a_volume_added_or_changed_on_a_live_worker_is_reported():
    live = stored(objects.worker_deployment(target()))
    volumes = live["spec"]["template"]["spec"]["volumes"]
    volumes.append({"name": "host", "hostPath": {"path": "/"}})
    secrets = next(volume for volume in volumes if volume["name"] == "secrets")
    secrets.pop("emptyDir")
    secrets["hostPath"] = {"path": "/var/run"}
    assert objects.worker_drift(live, target()) == [
        "spec.template.spec.volumes[host]",
        "spec.template.spec.volumes[secrets].medium",
        "spec.template.spec.volumes[secrets].source",
    ]


def test_both_namespaces_deny_ingress_by_default():
    for namespace in ("weave-system", "weave-workers"):
        assert policies(rendered(), namespace)["weave-deny-ingress"] == {"podSelector": {}, "policyTypes": ["Ingress"]}


def test_secrets_moved_off_memory_are_reported():
    live = stored(objects.worker_deployment(target()))
    secrets = next(volume for volume in live["spec"]["template"]["spec"]["volumes"] if volume["name"] == "secrets")
    del secrets["emptyDir"]["medium"]
    assert objects.worker_drift(live, target()) == ["spec.template.spec.volumes[secrets].medium"]


def test_a_widened_secret_volume_mode_is_reported():
    live = stored(objects.worker_deployment(target()))
    identity = next(volume for volume in live["spec"]["template"]["spec"]["volumes"] if volume["name"] == "identity")
    identity["secret"]["defaultMode"] = 0o444
    assert objects.worker_drift(live, target()) == ["spec.template.spec.volumes[identity].defaultMode"]
