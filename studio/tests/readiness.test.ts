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
import { describe, expect, it } from "vitest";
import type { Identity } from "../src/app/connection";
import {
  grantedIn,
  integrationReadiness,
  isLoopbackPlatform,
  type ReadinessFacts,
} from "../src/app/integrations/readiness";

const digest =
  "eddfa829184f8505fd0e1bc7a84b490fc57b555a39495b9724b2728b277133d8";
const versionId = "7a8cbeef-7d5c-483a-b926-4157ad4286d0";
const releaseId = "3f2a9b1c-0000-4000-8000-000000000001";
const profile = {
  baseUrl: "https://weave.example.com",
  tenantId: "tenant",
  projectId: "project",
  environmentId: "development",
};
const identity = (
  capabilities: string[],
  resources: string[] = [],
): Identity => ({
  principal_id: "me",
  kind: "human",
  grants: [
    {
      role: "test",
      scope: {
        tenant_id: "tenant",
        project_id: "project",
        environment_id: null,
      },
      resources,
      capabilities,
    },
  ],
  workspaces: [],
  truncated: false,
});
const everything = [
  "definition.publish",
  "connection.manage",
  "release.activate",
  "run.start",
];
const release = {
  id: releaseId,
  image_digest: "sha256:" + "a".repeat(64),
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
};
const ready: ReadinessFacts = {
  identity: identity(everything),
  profile,
  adapters: { state: "ok", value: ["weave-http-v2"] },
  descriptor: {
    state: "ok",
    value: {
      adapter: "weave-http-v2",
      reference: "weave-http@2.0.0",
      digest,
      published_version_id: versionId,
    },
  },
  connectorVersions: { state: "ok", value: [] },
  releases: { state: "ok", value: [release] },
};
const step = (facts: ReadinessFacts, id: string) =>
  integrationReadiness(facts).steps.find((s) => s.id === id)!;

describe("integration readiness", () => {
  it("looks ready when the connector is installed, published and released", () => {
    const result = integrationReadiness(ready);
    expect(result.looksReady).toBe(true);
    expect(result.connectorVersionId).toBe(versionId);
    expect(result.releaseIds).toEqual([releaseId]);
    expect(result.summary).toContain("Looks ready");
    // The API action builder's one line (W3-12).
    expect(result.short).toBe("Ready to publish");
    expect(
      result.steps.filter((s) => s.status === "action").map((s) => s.id),
    ).toEqual([]);
  });
  it("names the administrator for each missing grant", () => {
    const facts = { ...ready, identity: identity(["definition.publish"]) };
    expect(step(facts, "publish").status).toBe("done");
    expect(step(facts, "connections")).toMatchObject({
      status: "action",
      who: "Workspace administrator",
    });
    expect(step(facts, "activate")).toMatchObject({ who: "Deployer" });
    expect(step(facts, "run")).toMatchObject({ who: "Operator" });
    // Grants never decide whether the platform side looks ready.
    expect(integrationReadiness(facts).looksReady).toBe(true);
  });
  it("does not count a grant limited to specific resources or another project", () => {
    expect(
      grantedIn(identity(everything, ["some-resource"]), profile, "run.start"),
    ).toBe(false);
    expect(
      grantedIn(
        identity(everything),
        { ...profile, projectId: "other" },
        "run.start",
      ),
    ).toBe(false);
    expect(grantedIn(null, profile, "run.start")).toBe(false);
    expect(step({ ...ready, identity: null }, "publish").status).toBe(
      "unknown",
    );
  });
  it("asks the operator to install, publish and release on a remote platform", () => {
    const missing: ReadinessFacts = {
      ...ready,
      adapters: { state: "ok", value: ["weave-http"] },
      descriptor: { state: "ok", value: null },
      releases: { state: "ok", value: [] },
    };
    const result = integrationReadiness(missing);
    expect(result.looksReady).toBe(false);
    expect(step(missing, "installed")).toMatchObject({
      status: "action",
      who: "Platform operator",
    });
    expect(step(missing, "connector").status).toBe("action");
    expect(result.summary).toMatch(/steps need someone to act/);
    expect(result.short).toMatch(/^\d+ things to set up$/);
  });
  it("tells the operator to publish the installed connector", () => {
    const unpublished: ReadinessFacts = {
      ...ready,
      descriptor: {
        state: "ok",
        value: {
          ...(ready.descriptor as { value: never }).value,
          published_version_id: null,
        },
      },
    };
    // The installed digest is known, so its release still counts.
    expect(step(unpublished, "release").status).toBe("done");
    const connector = step(unpublished, "connector");
    expect(connector).toMatchObject({
      status: "action",
      who: "Developer or platform operator",
      command: "weave connector descriptor weave-http-v2 --output json",
    });
    // No published version means no digest to pin a release against.
    expect(integrationReadiness(unpublished).connectorVersionId).toBe("");
  });
  it("falls back to the project's published connectors", () => {
    const facts: ReadinessFacts = {
      ...ready,
      descriptor: { state: "unavailable", code: "WV-NOT-FOUND" },
      connectorVersions: {
        state: "ok",
        value: [
          {
            id: versionId,
            name: "weave-http",
            version: "2.0.0",
            definition_digest: digest,
            retired: false,
          },
        ],
      },
    };
    const result = integrationReadiness(facts);
    expect(result.connectorVersionId).toBe(versionId);
    expect(result.connectorDigest).toBe(digest);
    expect(result.looksReady).toBe(true);
  });
  it("trusts the installed adapters when the descriptor endpoint is missing", () => {
    const older: ReadinessFacts = {
      ...ready,
      descriptor: { state: "ok", value: null },
      connectorVersions: { state: "ok", value: [] },
    };
    expect(step(older, "installed").status).toBe("done");
    expect(step(older, "connector").status).toBe("action");
    // Releases are listed, but without any known digest none can match yet.
    expect(step(older, "release")).toMatchObject({
      status: "unknown",
      detail:
        "Studio checks this once weave-http@2.0.0 is published in this project.",
    });
  });
  it("on the local platform, you enable integrations on this computer", () => {
    const local: ReadinessFacts = {
      ...ready,
      identity: identity(["definition.publish"]),
      profile: { ...profile, baseUrl: "http://127.0.0.1:8080" },
      descriptor: {
        state: "ok",
        value: {
          ...(ready.descriptor as { value: never }).value,
          published_version_id: null,
        },
      },
      releases: { state: "ok", value: [] },
    };
    const result = integrationReadiness(local);
    expect(result.local).toBe(true);
    expect(step(local, "connector")).toMatchObject({
      who: "You, on this computer",
      command: "weave platform integrations enable",
    });
    expect(step(local, "connections").command).toContain("--role tenant_admin");
    expect(step(local, "secrets").command).toBe(
      "weave platform secret set --handle NAME",
    );
    expect(step(local, "access").command).toContain(
      "weave platform integrations grant",
    );
  });
  it("on the local platform with a release, it looks enabled and mentions the restart", () => {
    const local: ReadinessFacts = {
      ...ready,
      profile: { ...profile, baseUrl: "http://localhost:8080" },
    };
    expect(step(local, "release")).toMatchObject({
      status: "done",
      title: "Connector actions are enabled on this computer",
    });
    expect(step(local, "release").detail).toContain("restart");
  });
  it("reports refusals and loading instead of guessing", () => {
    const facts: ReadinessFacts = {
      ...ready,
      adapters: { state: "forbidden" },
      descriptor: { state: "forbidden" },
      connectorVersions: { state: "forbidden" },
      releases: { state: "pending" },
    };
    const result = integrationReadiness(facts);
    expect(result.checking).toBe(true);
    expect(result.looksReady).toBe(false);
    expect(step(facts, "installed").status).toBe("unknown");
    expect(step(facts, "release").detail).toBe("Checking…");
    expect(result.summary).toBe("Checking what this platform needs…");
    expect(result.short).toBe("Checking what this platform needs…");
  });
  it("recognizes loopback platforms only", () => {
    expect(isLoopbackPlatform("http://127.0.0.1:8080")).toBe(true);
    expect(isLoopbackPlatform("http://[::1]:8080")).toBe(true);
    expect(isLoopbackPlatform("http://studio.localhost")).toBe(true);
    expect(isLoopbackPlatform("https://weave.example.com")).toBe(false);
    expect(isLoopbackPlatform("https://127.0.0.1.example.com")).toBe(false);
    expect(isLoopbackPlatform("not a url")).toBe(false);
    expect(isLoopbackPlatform(null)).toBe(false);
  });
});
