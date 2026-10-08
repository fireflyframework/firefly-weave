/*
Copyright 2026 Firefly Software Foundation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0
*/
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { python, pythonAvailable } from "./python-path";
import { describe, expect, it } from "vitest";
import {
  activationPins,
  assignmentCandidates,
  assignmentLabel,
  connectorKey,
  connectorReleaseCandidates,
  connectorVersionFor,
  onlyChoice,
  readAssignmentBindings,
  readConnectorVersions,
  readDescriptor,
  readReleases,
  releaseLabel,
  requirementsFromExport,
  requirementsFromWorkflow,
  resolveConnector,
  workerReleaseCandidates,
} from "../src/app/integrations/activation-requirements";
import { preferredConnection } from "../src/app/integrations/connection-choice";

const digest =
  "eddfa829184f8505fd0e1bc7a84b490fc57b555a39495b9724b2728b277133d8";
const other = "1".repeat(64);
const version = "7a8cbeef-7d5c-483a-b926-4157ad4286d0";
const release = "3f2a9b1c-0000-4000-8000-000000000001";
const workerRelease = "3f2a9b1c-0000-4000-8000-000000000002";
const binding = "3f2a9b1c-0000-4000-8000-000000000003";

/** The shape of a definitions.export response, trimmed to the fields Studio reads. */
const exported = {
  id: "11111111-1111-4111-8111-111111111111",
  document: {
    spec: { connections: { pets: { connector: "weave-http@2.0.0" } } },
  },
  artifact: {
    executable: {
      connections: {
        pets: { connector: "weave-http@2.0.0", required: true },
        audit: { connector: "weave-http@2.0.0", required: false },
      },
      dependencies: [
        {
          kind: "Action",
          reference: "crm.lookup@2.0.0",
          digest: "2".repeat(64),
          document: {
            spec: {
              implementation: {
                kind: "worker",
                taskType: "crm-lookup",
                taskVersion: "1.0.0",
              },
            },
          },
        },
        {
          kind: "Action",
          reference: "get-pet@1.0.0",
          digest: "3".repeat(64),
          document: {
            spec: {
              implementation: {
                kind: "connector",
                uses: "weave-http@2.0.0",
                action: "read",
              },
            },
          },
        },
        {
          kind: "Action",
          reference: "add-pet@1.0.0",
          digest: "4".repeat(64),
          document: {
            spec: {
              implementation: {
                kind: "connector",
                uses: "weave-http@2.0.0",
                action: "write",
              },
            },
          },
        },
        {
          kind: "Connector",
          reference: "weave-http@2.0.0",
          digest,
          document: { spec: { adapter: "weave-http-v2" } },
        },
        { kind: "Adapter", reference: "weave-http-v2", digest: "5".repeat(64) },
      ],
      graph: {
        nodes: [
          { kind: "start", id: "@start" },
          { kind: "action", id: "fetch" },
          { kind: "humanTask", id: "review", assignment: "reviewers" },
          { kind: "humanTask", id: "second", assignment: "reviewers" },
          { kind: "humanTask", id: "audit", assignment: "auditors" },
        ],
      },
    },
  },
};

const releases = readReleases([
  {
    id: release,
    image_digest: "sha256:" + "a".repeat(64),
    capabilities: [
      { taskType: "weave-connector-http-read", taskVersion: "2.0.0" },
      { taskType: "weave-connector-http-write", taskVersion: "2.0.0" },
    ],
    connector_bindings: [
      {
        connector_digest: digest,
        action: "read",
        adapter: "weave-http-v2",
        implementation_version: "2.0.0",
        task_reference: "weave-connector-http-read@2.0.0",
      },
      {
        connector_digest: digest,
        action: "write",
        adapter: "weave-http-v2",
        implementation_version: "2.0.0",
        task_reference: "weave-connector-http-write@2.0.0",
      },
    ],
  },
  {
    // Read only: cannot serve a workflow that also writes.
    id: "3f2a9b1c-0000-4000-8000-000000000009",
    image_digest: "sha256:" + "b".repeat(64),
    capabilities: [
      { taskType: "weave-connector-http-read", taskVersion: "2.0.0" },
    ],
    connector_bindings: [
      {
        connector_digest: digest,
        action: "read",
        adapter: "weave-http-v2",
        task_reference: "weave-connector-http-read@2.0.0",
      },
    ],
  },
  {
    id: workerRelease,
    image_digest: "sha256:" + "c".repeat(64),
    capabilities: [{ taskType: "crm-lookup", taskVersion: "1.0.0" }],
  },
  { id: "not-a-uuid", capabilities: [] },
]);

describe("activation requirements from a published export", () => {
  const requirements = requirementsFromExport(exported)!;
  it("groups connector actions by connector digest", () => {
    expect(requirements.source).toBe("artifact");
    expect(requirements.connectors).toEqual([
      {
        reference: "weave-http@2.0.0",
        digest,
        adapter: "weave-http-v2",
        actions: ["read", "write"],
        usedBy: ["add-pet@1.0.0", "get-pet@1.0.0"],
      },
    ]);
  });
  it("lists worker task types and human assignments once", () => {
    expect(requirements.workers).toEqual([
      {
        taskType: "crm-lookup",
        versions: ["1.0.0"],
        usedBy: ["crm.lookup@2.0.0"],
      },
    ]);
    expect(requirements.assignments).toEqual(["auditors", "reviewers"]);
    expect(requirements.slots).toEqual([
      { name: "audit", connector: "weave-http@2.0.0", required: false },
      { name: "pets", connector: "weave-http@2.0.0", required: true },
    ]);
  });
  it("returns null for anything that is not a compiled export", () => {
    expect(requirementsFromExport({ items: [], next_cursor: null })).toBeNull();
    expect(requirementsFromExport(null)).toBeNull();
    expect(requirementsFromExport("x")).toBeNull();
  });
});

describe("matching environment releases and bindings", () => {
  const [connector] = requirementsFromExport(exported)!.connectors;
  it("offers only releases that bind every action of the exact digest", () => {
    expect(
      connectorReleaseCandidates(connector, releases).map((r) => r.id),
    ).toEqual([release]);
    expect(
      connectorReleaseCandidates({ ...connector, digest: other }, releases),
    ).toEqual([]);
    // A binding whose task capability the release does not offer is ignored.
    const broken = releases.map((r) => ({ ...r, capabilities: [] }));
    expect(connectorReleaseCandidates(connector, broken)).toEqual([]);
  });
  it("matches worker releases by every required task version", () => {
    const [worker] = requirementsFromExport(exported)!.workers;
    expect(workerReleaseCandidates(worker, releases).map((r) => r.id)).toEqual([
      workerRelease,
    ]);
    expect(
      workerReleaseCandidates({ ...worker, versions: ["2.0.0"] }, releases),
    ).toEqual([]);
  });
  it("matches enabled assignment bindings by name", () => {
    const bindings = readAssignmentBindings([
      {
        binding_id: binding,
        name: "reviewers",
        enabled: true,
        revision: 2,
        principal_ids: ["a"],
        group_ids: ["b", "c"],
      },
      {
        binding_id: "3f2a9b1c-0000-4000-8000-000000000004",
        name: "auditors",
        enabled: false,
        revision: 1,
      },
    ]);
    expect(
      assignmentCandidates("reviewers", bindings).map((b) => b.binding_id),
    ).toEqual([binding]);
    expect(assignmentCandidates("auditors", bindings)).toEqual([]);
    expect(assignmentLabel(bindings[0])).toBe(
      "reviewers · revision 2 · 1 person, 2 groups",
    );
  });
  it("auto-selects only a unique match", () => {
    expect(onlyChoice([release])).toBe(release);
    expect(onlyChoice([])).toBe("");
    expect(onlyChoice([release, workerRelease])).toBe("");
    expect(releaseLabel(releases[0])).toBe("Version 2.0.0");
    expect(
      releaseLabel({ ...releases[0], created_at: "2026-10-03T12:00:00Z" }),
    ).toBe("Version 2.0.0 · 2026-10-03");
  });
});

describe("connector version resolution", () => {
  const [connector] = requirementsFromExport(exported)!.connectors;
  const descriptor = readDescriptor({
    adapter: "weave-http-v2",
    reference: "weave-http@2.0.0",
    digest,
    published_version_id: version,
    manifest: {},
  });
  const versions = readConnectorVersions([
    {
      id: "6a8cbeef-7d5c-483a-b926-4157ad4286d0",
      kind: "Connector",
      name: "weave-http",
      version: "2.0.0",
      definition_digest: digest,
    },
    { id: "6a8cbeef-7d5c-483a-b926-4157ad4286d1", unavailable: true },
  ]);
  it("prefers the descriptor's published version", () => {
    expect(connectorVersionFor(connector, descriptor, versions)).toEqual({
      id: version,
      via: "descriptor",
    });
  });
  it("falls back to the project's published connectors", () => {
    expect(connectorVersionFor(connector, null, versions)).toEqual({
      id: "6a8cbeef-7d5c-483a-b926-4157ad4286d0",
      via: "catalog",
    });
    expect(versions).toHaveLength(1);
  });
  it("explains an unpublished or changed connector", () => {
    expect(connectorVersionFor(connector, null, [])).toEqual({
      problem: "not-published",
    });
    expect(
      connectorVersionFor(
        connector,
        { ...descriptor!, digest: other, published_version_id: null },
        [],
      ),
    ).toEqual({ problem: "installed-mismatch" });
    expect(readDescriptor({ items: [] })).toBeNull();
  });
  it("resolves slot-derived requirements through the published connectors", () => {
    const fallback = requirementsFromWorkflow(
      [{ name: "pets", connector: "weave-http@2.0.0", required: true }],
      ["crm-lookup", "bad name"],
      ["reviewers"],
    );
    expect(fallback.source).toBe("workflow");
    expect(fallback.workers.map((w) => w.taskType)).toEqual(["crm-lookup"]);
    const resolved = resolveConnector(fallback.connectors[0], versions);
    expect(resolved.digest).toBe(digest);
    expect(
      connectorReleaseCandidates(resolved, releases).map((r) => r.id),
    ).toEqual([release, "3f2a9b1c-0000-4000-8000-000000000009"]);
  });
});

describe("activation pins", () => {
  const requirements = requirementsFromExport(exported)!;
  const key = connectorKey(requirements.connectors[0]);
  const complete = {
    connectorVersions: { [key]: version },
    connectorReleases: { [key]: release },
    workers: { "crm-lookup": workerRelease },
    assignments: { reviewers: binding, auditors: binding },
  };
  it("sends connector_release_ids keyed by the connector version", () => {
    expect(activationPins(requirements, complete)).toEqual({
      connector_release_ids: { [version]: release },
      worker_release_ids: { "crm-lookup": workerRelease },
      assignment_binding_ids: { reviewers: binding, auditors: binding },
    });
  });
  it("names the first missing pin of an exact export", () => {
    expect(
      activationPins(requirements, { ...complete, connectorReleases: {} }),
    ).toEqual({
      problem: expect.stringContaining(
        "Choose a connector release for weave-http@2.0.0",
      ),
    });
    expect(
      activationPins(requirements, {
        ...complete,
        workers: { "crm-lookup": "x" },
      }),
    ).toEqual({
      problem: expect.stringContaining(
        "Choose a worker release for crm-lookup",
      ),
    });
    expect(
      activationPins(requirements, { ...complete, assignments: {} }),
    ).toEqual({
      problem: expect.stringContaining("auditors"),
    });
  });
  it("leaves unknown rows of an editor-derived workflow to the server", () => {
    const fallback = requirementsFromWorkflow(
      [{ name: "crm", connector: "crm@1.0.0", required: true }],
      ["crm-lookup"],
      ["reviewers"],
    );
    expect(
      activationPins(fallback, {
        // A resolved version alone is no pin: the slot may serve a worker.
        connectorVersions: { "crm@1.0.0": version },
        connectorReleases: {},
        workers: {},
        assignments: {},
      }),
    ).toEqual({
      connector_release_ids: {},
      worker_release_ids: {},
      assignment_binding_ids: {},
    });
  });
});

// The Python compiler is the authority on artifact field names: compile a
// workflow with a weave-http@2.0.0 Action, a worker Action and a human task,
// then read its envelope exactly as definitions.export returns it.
const root = resolve(import.meta.dirname, "../..");
const available = pythonAvailable();
describe.skipIf(!available)("parity with a compiled artifact", () => {
  it("derives the same pins the server admits", () => {
    const output = execFileSync(
      python,
      [
        "-c",
        `
import json
from uuid import uuid4
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR as D
from firefly_weave.contracts.workers import WorkerRelease

schema = {"type": "object"}
action = {"apiVersion": "weave/v1alpha1", "kind": "Action", "metadata": {"name": "get-pet", "version": "1.0.0"},
  "spec": {"implementation": {"kind": "connector", "uses": "weave-http@2.0.0", "action": "read",
    "config": {"profileVersion": "2.0.0", "method": "GET", "path": "/v1/pets/{petId}", "sideEffect": "read_only",
      "parameters": [{"name": "petId", "location": "path", "type": "string", "required": True}],
      "statuses": [200], "emptyStatuses": []}},
   "sideEffect": "read_only", "timeoutSeconds": 30,
   "inputSchema": {"type": "object", "properties": {"path": {"type": "object", "properties": {"petId": {"type": "string", "maxLength": 64}},
     "required": ["petId"], "additionalProperties": False}}, "required": ["path"], "additionalProperties": False},
   "outputSchema": {"type": "object", "properties": {"status": {"const": 200}, "body": schema}, "required": ["status", "body"]},
   "connection": {"connector": "weave-http@2.0.0"}}}
capability = {"taskType": "crm-lookup", "taskVersion": "1.0.0", "inputSchema": schema, "outputSchema": schema,
  "sideEffect": "idempotent", "timeoutSeconds": 60}
worker = {"apiVersion": "weave/v1alpha1", "kind": "Action", "metadata": {"name": "crm.lookup", "version": "2.0.0"},
  "spec": {"implementation": {"kind": "worker", "taskType": "crm-lookup", "taskVersion": "1.0.0"},
   "sideEffect": "idempotent", "timeoutSeconds": 60, "inputSchema": schema, "outputSchema": schema}}
workflow = {"apiVersion": "weave/v1alpha1", "kind": "Workflow", "metadata": {"name": "pets", "version": "1.0.0"},
  "spec": {"inputSchema": {"type": "object", "properties": {"petId": {"type": "string", "maxLength": 64}}, "required": ["petId"]},
   "outputSchema": schema, "timeoutSeconds": 3600, "connections": {"pets": {"connector": "weave-http@2.0.0"}},
   "steps": [
    {"id": "fetch", "kind": "action", "uses": "get-pet@1.0.0", "connection": "pets",
     "with": {"object": {"path": {"object": {"petId": {"ref": "/input/petId"}}}}}},
    {"id": "crm", "kind": "action", "uses": "crm.lookup@2.0.0", "with": {"literal": {}}},
    {"id": "review", "kind": "humanTask", "assignment": "reviewers", "title": {"literal": "Review"},
     "context": {"literal": {}}, "formSchema": schema, "decisions": ["approve", "reject"]}],
   "output": {"ref": "/steps/review/output"}}}
catalog = CatalogSnapshot.from_definitions(
    [load_definition(D.manifest.value), load_definition(action), load_definition(worker)],
    tasks=[capability], adapters=["weave-http-v2"])
result = compile_source(json.dumps(workflow), format="json", catalog=catalog)
assert result.artifact is not None, [d.message for d in result.diagnostics]
release = WorkerRelease(id=uuid4(), image_digest="sha256:" + "a" * 64, capabilities=list(D.capabilities),
    connector_bindings=list(D.bindings),
    credential_capabilities=[f"{c.task_type}@{c.task_version}" for c in D.capabilities])
print(json.dumps({"export": {"document": workflow, "artifact": json.loads(result.artifact.to_bytes())},
  "release": release.model_dump(mode="json", by_alias=True), "digest": D.manifest.digest}))
`,
      ],
      {
        cwd: root,
        encoding: "utf8",
        timeout: 180_000,
        env: { ...process.env, PYTHONPATH: resolve(root, "src") },
      },
    );
    const fixture = JSON.parse(output);
    const requirements = requirementsFromExport(fixture.export)!;
    expect(requirements.connectors).toEqual([
      {
        reference: "weave-http@2.0.0",
        digest: fixture.digest,
        adapter: "weave-http-v2",
        actions: ["read"],
        usedBy: ["get-pet@1.0.0"],
      },
    ]);
    expect(requirements.workers).toEqual([
      {
        taskType: "crm-lookup",
        versions: ["1.0.0"],
        usedBy: ["crm.lookup@2.0.0"],
      },
    ]);
    expect(requirements.assignments).toEqual(["reviewers"]);
    expect(requirements.slots).toEqual([
      { name: "pets", connector: "weave-http@2.0.0", required: true },
    ]);
    const releasesFromPython = readReleases([fixture.release]);
    expect(
      connectorReleaseCandidates(
        requirements.connectors[0],
        releasesFromPython,
      ),
    ).toHaveLength(1);
  });
});

describe("preferredConnection", () => {
  const connection = (id: string, name: string, revision: number) => ({
    id,
    name,
    revision,
    connector: "weave-http@2.0.0",
  });
  it("prefers the latest revision of the connection named like the slot", () => {
    expect(
      preferredConnection("todos", [
        connection("a", "jsonplaceholder", 1),
        connection("b", "todos", 1),
        connection("c", "todos", 3),
        connection("d", "todos", 2),
      ]),
    ).toBe("c");
  });
  it("falls back to the only compatible connection", () => {
    expect(
      preferredConnection("api", [connection("a", "jsonplaceholder", 1)]),
    ).toBe("a");
  });
  it("leaves the choice to the person when several could fit", () => {
    expect(
      preferredConnection("api", [
        connection("a", "jsonplaceholder", 1),
        connection("b", "todos", 1),
      ]),
    ).toBe("");
    expect(preferredConnection("api", [])).toBe("");
  });
});
