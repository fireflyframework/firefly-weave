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
// After an activation succeeds, Studio sets up its triggers row by row:
// creates what's missing, moves what this workflow ran to the new
// activation, stops what the person ticked, and keeps going past a failure,
// which never undoes the activation. Retry reads the resource again first.
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import type { TriggerRow } from "../src/app/editor/activation/trigger-plan";
import type {
  TriggerApi,
  TriggerResource,
} from "../src/app/editor/activation/trigger-resources-api";
import {
  attention,
  connectionNeed,
  retryResult,
  settingsKey,
  syncTriggers,
  withStoredResources,
  type SyncContext,
} from "../src/app/editor/activation/trigger-sync";
import type { TriggerIntent } from "../src/app/editor/state/canvas-sidecar";

const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";

class FakeApi implements TriggerApi {
  readonly environment = ENV;
  readonly calls: {
    method: string;
    path: string;
    body?: unknown;
    revision?: number;
  }[] = [];
  answers: unknown[] = [];
  async page() {
    return { items: [], next_cursor: null };
  }
  async request<T>(path: string, method = "GET", body?: unknown) {
    this.calls.push({ method, path: path.slice(ENV.length), body });
    return this.next() as T;
  }
  async mutate<T>(
    path: string,
    method: string,
    body: unknown,
    revision?: number,
  ) {
    this.calls.push({ method, path: path.slice(ENV.length), body, revision });
    return this.next() as T;
  }
  private next() {
    const answer = this.answers.shift();
    if (answer instanceof Error) throw answer;
    return answer;
  }
}

const HOOK: TriggerIntent = {
  id: "trigger-1b2c3d4e",
  kind: "webhook",
  name: "orders-in",
  secretRef: "signing",
};
const OLD: TriggerIntent = {
  id: "trigger-2c3d4e5f",
  kind: "webhook",
  name: "orders-old",
  secretRef: "signing",
};
const view = (
  id: string,
  name: string,
  revision: number,
  activation = "a-1",
) => ({
  id,
  name,
  kind: "run",
  activation_id: activation,
  secret_ref: "signing",
  payload_schema: {},
  max_body_bytes: 1048576,
  tolerance_seconds: 300,
  principal_id: "p-1",
  disabled: false,
  revision,
});
const resource = (
  id: string,
  name: string,
  revision: number,
): TriggerResource => ({
  kind: "webhook",
  id,
  name,
  activationId: "a-1",
  revision,
  disabled: false,
  view: view(id, name, revision) as never,
});
const context = (
  api: FakeApi,
  intents: TriggerIntent[] = [HOOK],
): SyncContext => ({
  api,
  activationId: "a-2",
  environmentId: "env-1",
  inputSchema: { type: "object" },
  intents,
  connections: {},
});

describe("setting up triggers after an activation", () => {
  it("runs each row in order and says what it did", async () => {
    const api = new FakeApi();
    api.answers = [
      view("w-new", "orders-in", 1, "a-2"),
      view("w-1", "orders-x", 4, "a-2"),
      { disabled: true },
    ];
    const rows: TriggerRow[] = [
      {
        kind: "create",
        intentId: HOOK.id,
        resourceKind: "webhook",
        text: "Create webhook orders-in",
      },
      {
        kind: "retarget",
        intentId: OLD.id,
        resourceKind: "webhook",
        resource: resource("w-1", "orders-x", 3),
        from: "1.1.0",
        to: "1.2.0",
        text: "Move…",
      },
      {
        kind: "left-behind",
        resourceKind: "webhook",
        resource: resource("w-9", "orders-gone", 2),
        from: "1.1.0",
        stop: true,
        text: "Webhook orders-gone keeps running on 1.1.0",
      },
      {
        kind: "left-behind",
        resourceKind: "webhook",
        resource: resource("w-8", "orders-kept", 2),
        from: "1.1.0",
        stop: false,
        text: "…",
      },
      {
        kind: "called",
        intentId: "trigger-3d4e5f6a",
        text: "Callers pin this version when they activate",
      },
    ];
    const results = await syncTriggers(rows, context(api, [HOOK, OLD]));
    expect(
      results.map((result) => [result.status, result.text, result.resourceId]),
    ).toEqual([
      // Each row a receipt: what happened, to which trigger.
      ["created", "Created webhook orders-in", "w-new"],
      [
        "moved",
        "Moved webhook orders-x to 1.2.0 (the URL stays the same)",
        "w-1",
      ],
      ["stopped", "Stopped webhook orders-gone", "w-9"],
    ]);
    expect(
      api.calls.map((call) =>
        `${call.method} ${call.path} ${call.revision ?? ""}`.trim(),
      ),
    ).toEqual([
      "POST /triggers",
      "POST /triggers/w-1/retarget 3",
      "POST /triggers/w-9/disable",
    ]);
    expect(attention(results)).toBeNull();
  });

  it("keeps going past a failure, says why, and counts it", async () => {
    const api = new FakeApi();
    api.answers = [
      new ApiError(409, {
        code: "WV-REVISION",
        message: "The trigger changed; read it again before moving it",
      }),
      view("w-new", "orders-in", 1, "a-2"),
    ];
    const rows: TriggerRow[] = [
      {
        kind: "retarget",
        intentId: OLD.id,
        resourceKind: "webhook",
        resource: resource("w-1", "orders-old", 3),
        from: "1.1.0",
        to: "1.2.0",
        text: "Move…",
      },
      {
        kind: "create",
        intentId: HOOK.id,
        resourceKind: "webhook",
        text: "Create webhook orders-in",
      },
    ];
    const results = await syncTriggers(rows, context(api, [HOOK, OLD]));
    expect(results.map((result) => [result.status, result.text])).toEqual([
      [
        "failed",
        "Couldn't move webhook orders-old: Someone else changed this item. Reload it to see the latest version, then try again.",
      ],
      ["created", "Created webhook orders-in"],
    ]);
    expect(attention(results)).toBe("1 trigger needs attention.");
  });

  it("names a moved schedule by its calendar and the version it moved to", async () => {
    const api = new FakeApi();
    const schedule: TriggerIntent = {
      id: "trigger-4e5f6a7b",
      kind: "schedule",
      cron: "0 9 * * 1",
      input: {},
    };
    const scheduleView = {
      id: "s-1",
      cron: "0 9 * * 1",
      timezone: "UTC",
      missed_policy: "skip",
      activation_id: "a-1",
      input: {},
      revision: 2,
      principal_id: "p-1",
      status: "enabled",
      next_due_at: "2026-11-23T09:00:00Z",
      blocked_reason: null,
    };
    api.answers = [{ ...scheduleView, activation_id: "a-2", revision: 3 }];
    const [result] = await syncTriggers(
      [
        {
          kind: "schedule-move",
          intentId: schedule.id,
          resourceKind: "schedule",
          resource: {
            kind: "schedule",
            id: "s-1",
            name: "0 9 * * 1",
            activationId: "a-1",
            revision: 2,
            disabled: false,
            view: scheduleView as never,
          },
          from: "1.1.0",
          to: "1.2.0",
          text: "Move schedule…",
        },
      ],
      context(api, [schedule]),
    );
    expect(result).toMatchObject({
      status: "moved",
      text: expect.stringMatching(/^Moved schedule .+ to 1\.2\.0$/),
    });
    expect(result.text).not.toContain("0 9 * * 1");
  });

  it("retries a move with the revision it reads again", async () => {
    const api = new FakeApi();
    api.answers = [
      view("w-1", "orders-old", 5),
      view("w-1", "orders-old", 6, "a-2"),
    ];
    const failed = {
      row: {
        kind: "retarget",
        intentId: OLD.id,
        resourceKind: "webhook",
        resource: resource("w-1", "orders-old", 3),
        from: "1.1.0",
        to: "1.2.0",
        text: "Move…",
      } as TriggerRow,
      status: "failed" as const,
      text: "Couldn't move webhook orders-old: changed",
    };
    expect(await retryResult(failed, context(api, [OLD]))).toMatchObject({
      status: "moved",
      text: "Moved webhook orders-old to 1.2.0 (the URL stays the same)",
      resourceId: "w-1",
    });
    expect(
      api.calls.map((call) =>
        `${call.method} ${call.path} ${call.revision ?? ""}`.trim(),
      ),
    ).toEqual(["GET /triggers/w-1", "POST /triggers/w-1/retarget 5"]);
  });

  it("names what it couldn't create, and a trigger choice that's gone", async () => {
    const api = new FakeApi();
    api.answers = [
      new ApiError(403, {
        code: "WV-FORBIDDEN",
        message: "You can't manage triggers here",
      }),
    ];
    const results = await syncTriggers(
      [
        {
          kind: "create",
          intentId: HOOK.id,
          resourceKind: "webhook",
          text: "Create webhook orders-in",
        },
        {
          kind: "create",
          intentId: "trigger-99999999",
          resourceKind: "webhook",
          text: "Create webhook gone",
        },
      ],
      context(api),
    );
    expect(results.map((result) => result.text)).toEqual([
      expect.stringMatching(/^Couldn't create webhook orders-in: /),
      "Couldn't create this trigger: its trigger choice is no longer in the workflow.",
    ]);
    expect(attention(results)).toBe("2 triggers need attention.");
  });
});

describe("the connection a new trigger runs on", () => {
  it("is any version of the Kafka or email connector, or the app's exact package", () => {
    expect(
      connectionNeed({ id: "t-1", kind: "broker", name: "orders" }),
    ).toEqual({
      connector: "weave-kafka",
      label: "Kafka trigger orders",
    });
    expect(connectionNeed({ id: "t-2", kind: "email" })).toEqual({
      connector: "weave-email",
      label: "Email trigger",
    });
    expect(
      connectionNeed({
        id: "t-3",
        kind: "provider",
        name: "support-chat",
        provider: "whatsapp",
        package: "weave-whatsapp",
        packageVersion: "1.2.0",
        schemaDigest: "d".repeat(64),
      }),
    ).toEqual({
      connector: "weave-whatsapp@1.2.0",
      label: "App event trigger support-chat",
    });
    expect(connectionNeed(HOOK)).toBeNull();
    expect(
      connectionNeed({ id: "t-4", kind: "schedule", cron: "0 9 * * 1" }),
    ).toBeNull();
  });

  it("creates a Kafka trigger on the connection chosen for it", async () => {
    const api = new FakeApi();
    const kafka: TriggerIntent = {
      id: "trigger-3d4e5f6a",
      kind: "broker",
      name: "orders",
      clusterId: "orders-cluster",
      topic: "orders",
      deadLetterPolicy: "receipt",
    };
    api.answers = [
      {
        id: "b-1",
        name: "orders",
        kind: "run",
        activation_id: "a-2",
        connection_revision_id: "rev-k",
        cluster_id: "orders-cluster",
        topic: "orders",
        revision: 1,
        disabled: false,
      },
    ];
    const [result] = await syncTriggers(
      [
        {
          kind: "create",
          intentId: kafka.id,
          resourceKind: "broker",
          text: "Create Kafka trigger orders",
        },
      ],
      { ...context(api, [kafka]), connections: { [kafka.id]: "rev-k" } },
    );
    expect(result).toMatchObject({ status: "created", resourceId: "b-1" });
    expect(api.calls[0]).toMatchObject({
      method: "POST",
      path: "/broker-triggers",
      body: expect.objectContaining({ connection_revision_id: "rev-k" }),
    });
  });
});

describe("what the workflow keeps", () => {
  it("stores each created or moved resource in its trigger choice for this environment", () => {
    const intents = [{ ...HOOK, resources: { "env-0": "w-elsewhere" } }, OLD];
    const kept = withStoredResources(intents, "env-1", [
      {
        row: {
          kind: "create",
          intentId: HOOK.id,
          resourceKind: "webhook",
          text: "",
        },
        status: "created",
        text: "Created",
        resourceId: "w-new",
      },
      {
        row: {
          kind: "retarget",
          intentId: OLD.id,
          resourceKind: "webhook",
          text: "",
        },
        status: "failed",
        text: "Couldn't move",
      },
    ]);
    expect(kept[0]).toMatchObject({
      resources: { "env-0": "w-elsewhere", "env-1": "w-new" },
    });
    expect(kept[0]["activated"]).toEqual({ "env-1": settingsKey(HOOK) });
    expect(kept[1]).toEqual(OLD);
  });

  it("keys a trigger choice's settings, not its stored IDs", () => {
    expect(
      settingsKey({ ...HOOK, resources: { e: "x" }, activated: { e: "k" } }),
    ).toBe(settingsKey(HOOK));
    expect(settingsKey({ ...HOOK, name: "orders-other" })).not.toBe(
      settingsKey(HOOK),
    );
  });
});
