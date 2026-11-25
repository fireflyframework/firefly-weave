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
import { ApiError, placeOf } from "../src/app/api";
import type { PendingStop } from "../src/app/editor/activation/event-bindings";
import {
  triggerPlan,
  withStop,
  type TriggerRow,
} from "../src/app/editor/activation/trigger-plan";
import type {
  TriggerApi,
  TriggerResource,
} from "../src/app/editor/activation/trigger-resources-api";
import {
  actionable,
  attention,
  chooseResult,
  connectionNeed,
  needsSync,
  retryResult,
  settingsKey,
  syncHeld,
  syncTriggers,
  withStoredResources,
  type SyncContext,
  type SyncResult,
} from "../src/app/editor/activation/trigger-sync";
import type { TriggerIntent } from "../src/app/editor/state/canvas-sidecar";
import { FakePlatform, refusal, requested, type Seed } from "./event-platform";

const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";

class FakeApi implements TriggerApi {
  environment = ENV;
  readonly expectedPrincipal = null;
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
  place: placeOf(api),
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

describe("another environment or account chosen meanwhile", () => {
  it("sets up, retries and stops nothing more there", async () => {
    const api = new FakeApi();
    const context_ = context(api, [HOOK, OLD]);
    const rows: TriggerRow[] = [
      {
        kind: "create",
        resourceKind: "webhook",
        intentId: HOOK.id,
        text: "Create webhook orders-in",
      },
      {
        kind: "left-behind",
        resourceKind: "webhook",
        resource: resource("w-9", "orders-legacy", 3),
        stop: true,
        text: "Webhook orders-legacy keeps running on 1.0.0",
      },
    ];
    api.answers = [new ApiError(500, { message: "Broken" })];
    const [failed] = await syncTriggers(rows, context_, () => {
      api.environment = `${ENV}-other`;
    });
    const sent = api.calls.length;
    const nothing = (name: string) =>
      `Studio did nothing to ${name}: the environment or the account changed since you opened this.`;
    expect(await retryResult(failed!, context_)).toMatchObject({
      status: "changed",
      text: nothing("webhook orders-in"),
    });
    expect((await syncTriggers(rows, context_))[1]).toMatchObject({
      status: "changed",
      text: nothing("webhook orders-legacy"),
    });
    expect(api.calls).toHaveLength(sent);
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
    expect(settingsKey({ ...HOOK, pendingStops: { e: [] } })).toBe(
      settingsKey(HOOK),
    );
    expect(settingsKey({ ...HOOK, name: "orders-other" })).not.toBe(
      settingsKey(HOOK),
    );
  });
});

describe("a platform event trigger after an activation", () => {
  const EVENT: TriggerIntent = { id: "trigger-5f6a7b8c", kind: "event" };
  const ATTENTION = {
    events: ["transaction.in_doubt" as const],
    from: ["move-between-systems"],
  };
  const WIDER = {
    events: [
      "transaction.compensated" as const,
      "transaction.in_doubt" as const,
    ],
    from: null,
  };
  const OLD_ONE: Seed = { id: "t-1", name: "notify-team", activation: "a-old" };
  const SECOND: Seed = {
    id: "t-2",
    name: "notify-team-2",
    activation: "a-older",
  };
  const seeded = (...seeds: Seed[]) => {
    const platform = new FakePlatform();
    for (const seed of seeds) platform.seed(seed);
    return platform;
  };
  const rowsFor = (
    platform: FakePlatform,
    intents: TriggerIntent[],
    declaration: typeof ATTENTION | typeof WIDER | null = ATTENTION,
  ): TriggerRow[] =>
    triggerPlan({
      intents,
      resources: {
        event: {
          items: platform.views().map((item) => ({
            kind: "event",
            id: item.id,
            name: item.name,
            activationId: item.activation_id,
            revision: item.revision,
            disabled: item.disabled,
            view: item,
          })),
          truncated: false,
        },
      },
      activations: [
        { id: "a-older", workflow: "notify-team", version: "1.0.0" },
        { id: "a-old", workflow: "notify-team", version: "1.1.0" },
        { id: "a-new", workflow: "notify-team", version: "1.2.0" },
      ],
      workflow: "notify-team",
      version: { version: "1.2.0", inputSchema: {} },
      declaration,
      environmentId: "env-1",
    });
  const within = (
    platform: FakePlatform,
    intents: TriggerIntent[],
  ): SyncContext => ({
    api: platform,
    place: placeOf(platform),
    activationId: "a-new",
    environmentId: "env-1",
    environment: "development",
    inputSchema: {},
    intents,
    connections: {},
  });
  const stored = (extra: Record<string, unknown> = {}): TriggerIntent => ({
    ...EVENT,
    resources: { "env-1": "t-1" },
    ...extra,
  });
  const keep = (
    intents: TriggerIntent[],
    results: SyncResult[],
    rows: TriggerRow[],
  ) => withStoredResources(intents, "env-1", results, rows)[0]!;
  const statuses = (results: SyncResult[]) =>
    results.map((result) => [result.status, result.text]);

  it("is created, kept for the environment, and named in its permissions refusal", async () => {
    const platform = seeded();
    const rows = rowsFor(platform, [EVENT]);
    expect(rows.map(actionable)).toEqual([true]);
    platform.capabilities.delete("run.start");
    const refused = await syncTriggers(rows, within(platform, [EVENT]));
    expect(statuses(refused)).toEqual([
      [
        "failed",
        "Couldn't create notify-team: you need to manage triggers, start runs and read runs in development.",
      ],
    ]);
    expect(attention(refused)).toBe("1 trigger needs attention.");
    expect(keep([EVENT], refused, rows)).toEqual(EVENT);
    platform.capabilities.add("run.start");
    const again = await retryResult(refused[0]!, within(platform, [EVENT]));
    expect(again).toMatchObject({
      status: "created",
      text: "Created notify-team",
      resourceId: "t-made-1",
    });
    expect(keep([EVENT], [again], rows)).toMatchObject({
      resources: { "env-1": "t-made-1" },
    });
    expect(attention([again])).toBeNull();
  });

  it("adopts a create whose answer was lost when Retry runs", async () => {
    const platform = seeded();
    platform.lose.add("POST /event-triggers");
    const rows = rowsFor(platform, [EVENT]);
    const [lost] = await syncTriggers(rows, within(platform, [EVENT]));
    expect(lost!.status).toBe("failed");
    expect(await retryResult(lost!, within(platform, [EVENT]))).toMatchObject({
      status: "created",
      resourceId: "t-made-1",
    });
    expect(platform.enabled()).toHaveLength(1);
  });

  it("is moved with the revision read", async () => {
    const platform = seeded(OLD_ONE);
    const intents = [stored()];
    const results = await syncTriggers(
      rowsFor(platform, intents),
      within(platform, intents),
    );
    expect(statuses(results)).toEqual([
      ["moved", "Moved notify-team to 1.2.0"],
    ]);
    expect(platform.sent.at(-1)).toMatchObject({
      key: "POST /event-triggers/t-1/retarget",
      revision: 1,
      body: { activation_id: "a-new" },
    });
  });

  it("is replaced, and the new one is kept for the environment", async () => {
    const platform = seeded(OLD_ONE);
    const intents = [stored()];
    const rows = rowsFor(platform, intents, WIDER);
    const results = await syncTriggers(rows, within(platform, intents));
    expect(statuses(results)).toEqual([
      ["replaced", "Replaced by notify-team-2"],
    ]);
    expect(attention(results)).toBeNull();
    const kept = keep(intents, results, rows);
    expect(kept).toMatchObject({ resources: { "env-1": "t-made-1" } });
    expect(kept).not.toHaveProperty("pendingStops");
  });

  it("keeps the new trigger and the stop it owes when the old one couldn't be stopped", async () => {
    const platform = seeded({ ...OLD_ONE, revision: 3 });
    platform.refuse.set(
      "POST /event-triggers/t-1/disable",
      refusal(500, "WV-INTERNAL", "Request failed"),
    );
    const intents = [stored()];
    const rows = rowsFor(platform, intents, WIDER);
    const results = await syncTriggers(rows, within(platform, intents));
    expect(results[0]).toMatchObject({
      status: "failed",
      event: { retry: "stop" },
    });
    expect(attention(results)).toBe("1 trigger needs attention.");
    const owed: PendingStop = {
      triggerId: "t-1",
      revision: 3,
      activationId: "a-old",
    };
    const kept = keep(intents, results, rows);
    expect(kept).toMatchObject({
      resources: { "env-1": "t-made-1" },
      pendingStops: { "env-1": [owed] },
    });
    // The next activation offers to stop it, on.
    const next = rowsFor(platform, [kept], WIDER);
    expect(next.map((row) => [row.kind, row.stop])).toEqual([
      ["retarget", undefined],
      ["duplicate", true],
    ]);
    // Stop it tries once more, and nothing is owed after it works.
    const again = await retryResult(results[0]!, within(platform, intents));
    expect(again).toMatchObject({
      status: "replaced",
      text: "Replaced by notify-team-2",
    });
    expect(platform.enabled()).toHaveLength(1);
    expect(keep([kept], [again], rows)).not.toHaveProperty("pendingStops");
  });

  it("asks before skipping waiting events; Done and Keep it for now owe the stop, Stop now doesn't", async () => {
    const platform = seeded(OLD_ONE);
    platform.starts.set("t-1", [requested("s-1")]);
    const intents = [stored()];
    const rows = rowsFor(platform, intents, WIDER);
    const context = within(platform, intents);
    const [asked] = await syncTriggers(rows, context);
    expect(asked).toMatchObject({ status: "waiting", resourceId: "t-made-1" });
    expect(attention([asked!])).toBe("1 trigger needs your choice.");
    const owed = {
      "env-1": [{ triggerId: "t-1", revision: 1, activationId: "a-old" }],
    };
    // Done without an answer.
    expect(keep(intents, [asked!], rows)["pendingStops"]).toEqual(owed);
    const kept = await chooseResult(asked!, "keep", context);
    expect(kept).toMatchObject({
      status: "kept",
      text: "Created notify-team-2. Kept notify-team for now.",
    });
    expect(attention([kept])).toBeNull();
    expect(keep(intents, [kept], rows)).toMatchObject({
      resources: { "env-1": "t-made-1" },
      pendingStops: owed,
    });
    expect(platform.writes()).not.toContain("POST /event-triggers/t-1/disable");
    const stopped = await chooseResult(asked!, "stop", context);
    expect(stopped).toMatchObject({ status: "replaced" });
    expect(keep(intents, [stopped], rows)).not.toHaveProperty("pendingStops");
  });

  it("stops a duplicate only after the declared trigger is set up", async () => {
    const platform = seeded(OLD_ONE, SECOND);
    platform.refuse.set(
      "POST /event-triggers/t-1/retarget",
      refusal(500, "WV-INTERNAL", "Request failed"),
    );
    const intents = [stored()];
    const rows = rowsFor(platform, intents);
    expect(rows.map((row) => row.kind)).toEqual(["retarget", "duplicate"]);
    const context = within(platform, intents);
    const results = await syncTriggers(rows, context);
    expect(
      results.map((result) => [result.status, result.event?.held]),
    ).toEqual([
      ["failed", undefined],
      ["kept", true],
    ]);
    expect(platform.writes()).toEqual(["POST /event-triggers/t-1/retarget"]);
    // It may be the only one reacting as declared: no stop is owed.
    expect(keep(intents, results, rows)).not.toHaveProperty("pendingStops");
    expect(attention(results)).toBe("1 trigger needs attention.");
    // Retry sets the declared trigger up, and the duplicate is stopped then.
    const byRow = new Map(results.map((result, index) => [index, result]));
    const seen: [number, string][] = [];
    const held = (index: number, result: SyncResult) =>
      seen.push([index, result.text]);
    await syncHeld(rows, byRow, context, held);
    expect(seen).toEqual([]);
    byRow.set(0, await retryResult(results[0]!, context));
    await syncHeld(rows, byRow, context, held);
    expect(seen).toEqual([[1, "Stopped notify-team-2"]]);
    expect(platform.enabled().map((item) => item["id"])).toEqual(["t-1"]);
  });

  it("holds a duplicate while the replaced trigger's events wait", async () => {
    const platform = seeded(OLD_ONE, {
      ...SECOND,
      events: ["transaction.compensated", "transaction.in_doubt"],
      sources: undefined,
    });
    platform.starts.set("t-1", [requested("s-1")]);
    const intents = [stored()];
    const rows = rowsFor(platform, intents, WIDER);
    expect(rows.map((row) => row.kind)).toEqual(["replace", "duplicate"]);
    const results = await syncTriggers(rows, within(platform, intents));
    expect(results.map((result) => result.status)).toEqual(["waiting", "kept"]);
    expect(platform.writes()).toEqual(["POST /event-triggers"]);
  });

  it("stops a duplicate beside a trigger it leaves as it is", async () => {
    const platform = seeded({ ...OLD_ONE, mayReconcile: null }, SECOND);
    const intents = [stored()];
    const rows = rowsFor(platform, intents);
    expect(rows.map((row) => row.kind)).toEqual(["blocked", "duplicate"]);
    const results = await syncTriggers(rows, within(platform, intents));
    expect(statuses(results)).toEqual([["stopped", "Stopped notify-team-2"]]);
  });

  it("owes the stop of a duplicate the person unticked beside a trigger it leaves as it is", async () => {
    const platform = seeded({ ...OLD_ONE, mayReconcile: null }, SECOND);
    const intents = [stored()];
    const rows = rowsFor(platform, intents).map((row) =>
      row.kind === "duplicate" ? withStop(row, false) : row,
    );
    expect(rows.map((row) => row.kind)).toEqual(["blocked", "duplicate"]);
    // Nothing is sent, but something is to be kept.
    expect(rows.some(actionable)).toBe(false);
    expect(needsSync(rows)).toBe(true);
    const results = await syncTriggers(rows, within(platform, intents));
    expect(platform.writes()).toEqual([]);
    expect(keep(intents, results, rows)["pendingStops"]).toEqual({
      "env-1": [{ triggerId: "t-2", revision: 1, activationId: "a-older" }],
    });
  });

  it("has nothing to do for a trigger it leaves as it is", () => {
    const platform = seeded({ ...OLD_ONE, mayReconcile: null });
    const rows = rowsFor(platform, [stored()]);
    expect(rows.map((row) => row.kind)).toEqual(["blocked"]);
    expect(needsSync(rows)).toBe(false);
  });

  it("keeps a duplicate the person unticked while it was held, once the declared trigger is set up", async () => {
    const platform = seeded(OLD_ONE, SECOND);
    platform.refuse.set(
      "POST /event-triggers/t-1/retarget",
      refusal(500, "WV-INTERNAL", "Request failed"),
    );
    const intents = [stored()];
    const rows = rowsFor(platform, intents).map((row) =>
      row.kind === "duplicate" ? withStop(row, false) : row,
    );
    const context = within(platform, intents);
    const results = await syncTriggers(rows, context);
    expect(results[1]!.event).toMatchObject({ held: true });
    const byRow = new Map(results.map((result, index) => [index, result]));
    byRow.set(0, await retryResult(results[0]!, context));
    await syncHeld(rows, byRow, context, (index, result) =>
      byRow.set(index, result),
    );
    expect(statuses([...byRow.values()])).toEqual([
      ["moved", "Moved notify-team to 1.2.0"],
      ["kept", "Kept notify-team-2 for now."],
    ]);
    expect(platform.find("t-2")!["disabled"]).toBe(false);
    expect(keep(intents, [...byRow.values()], rows)["pendingStops"]).toEqual({
      "env-1": [{ triggerId: "t-2", revision: 1, activationId: "a-older" }],
    });
  });

  const WHY: [string, (platform: FakePlatform) => void][] = [
    ["was stopped", (platform) => (platform.find("t-1")!["disabled"] = true)],
    [
      "moved to another version",
      (platform) => (platform.find("t-1")!["activation_id"] = "a-old"),
    ],
    [
      "starts another workflow",
      (platform) =>
        (platform.find("t-1")!["workflow_name"] = "sync-references"),
    ],
    [
      "is gone",
      (platform) =>
        platform.triggers.splice(
          platform.triggers.indexOf(platform.find("t-1")!),
          1,
        ),
    ],
  ];
  it.each(WHY)(
    "stops no duplicate later when the declared trigger %s",
    async (_what, change) => {
      const platform = seeded(OLD_ONE, SECOND);
      platform.starts.set("t-2", [requested("s-1")]);
      const intents = [stored()];
      const rows = rowsFor(platform, intents);
      const context = within(platform, intents);
      const results = await syncTriggers(rows, context);
      expect(results.map((result) => result.status)).toEqual([
        "moved",
        "waiting",
      ]);
      change(platform);
      const answered = await chooseResult(results[1]!, "stop", context);
      expect(statuses([answered])).toEqual([
        ["changed", "notify-team changed since you opened this. Review it."],
      ]);
      expect(platform.find("t-2")!["disabled"]).toBe(false);
      // It may be the only one that reacts now, and its stop stays owed.
      expect(keep(intents, [results[0]!, answered], rows)).toMatchObject({
        pendingStops: { "env-1": [{ triggerId: "t-2" }] },
      });
    },
  );

  it("stops no duplicate again after a failure when the declared trigger no longer reacts", async () => {
    const platform = seeded(OLD_ONE, SECOND);
    platform.refuse.set(
      "POST /event-triggers/t-2/disable",
      refusal(500, "WV-INTERNAL", "Request failed"),
    );
    const intents = [stored()];
    const rows = rowsFor(platform, intents);
    const context = within(platform, intents);
    const results = await syncTriggers(rows, context);
    expect(results[1]).toMatchObject({ status: "failed" });
    platform.find("t-1")!["disabled"] = true;
    expect(await retryResult(results[1]!, context)).toMatchObject({
      status: "changed",
    });
    expect(platform.find("t-2")!["disabled"]).toBe(false);
  });

  it("stops a duplicate beside a trigger it leaves as it is only while that one is as it was", async () => {
    const platform = seeded({ ...OLD_ONE, mayReconcile: null }, SECOND);
    const intents = [stored()];
    const rows = rowsFor(platform, intents);
    platform.find("t-1")!["revision"] = 9;
    const results = await syncTriggers(rows, within(platform, intents));
    expect(statuses(results)).toEqual([
      ["changed", "notify-team changed since you opened this. Review it."],
    ]);
    expect(platform.writes()).toEqual([]);
  });

  it("says why a duplicate wasn't stopped when the declared trigger can't be read, with Stop it", async () => {
    const platform = seeded(OLD_ONE, SECOND);
    const intents = [stored()];
    const rows = rowsFor(platform, intents);
    const context = within(platform, intents);
    platform.refuse.set(
      "GET /event-triggers/t-1",
      refusal(503, "WV-UNAVAILABLE", "Try again later"),
    );
    const results = await syncTriggers(rows, context);
    expect(results[1]).toMatchObject({
      status: "failed",
      text: "Couldn't stop notify-team-2: Try again later.",
      event: { retry: "stop", pendingStop: { triggerId: "t-2" } },
    });
    expect(await retryResult(results[1]!, context)).toMatchObject({
      status: "stopped",
    });
  });

  it("stops nothing later, and creates nothing, when the trigger a replace made no longer reacts", async () => {
    const platform = seeded(OLD_ONE);
    platform.starts.set("t-1", [requested("s-1")]);
    const intents = [stored()];
    const rows = rowsFor(platform, intents, WIDER);
    const context = within(platform, intents);
    const [asked] = await syncTriggers(rows, context);
    platform.find("t-made-1")!["disabled"] = true;
    const answered = await chooseResult(asked!, "stop", context);
    expect(statuses([answered])).toEqual([
      ["changed", "notify-team-2 changed since you opened this. Review it."],
    ]);
    expect(platform.enabled().map((item) => item["id"])).toEqual(["t-1"]);
    expect(platform.writes()).toEqual(["POST /event-triggers"]);
    expect(keep(intents, [answered], rows)).toMatchObject({
      resources: { "env-1": "t-made-1" },
      pendingStops: { "env-1": [{ triggerId: "t-1" }] },
    });
  });

  it("stops no held duplicate when the declared trigger stopped reacting once it was set up", async () => {
    const platform = seeded(OLD_ONE, SECOND);
    platform.refuse.set(
      "POST /event-triggers/t-1/retarget",
      refusal(500, "WV-INTERNAL", "Request failed"),
    );
    const intents = [stored()];
    const rows = rowsFor(platform, intents);
    const context = within(platform, intents);
    const results = await syncTriggers(rows, context);
    const byRow = new Map(results.map((result, index) => [index, result]));
    byRow.set(0, await retryResult(results[0]!, context));
    platform.find("t-1")!["disabled"] = true;
    const seen: SyncResult[] = [];
    await syncHeld(rows, byRow, context, (_index, result) => seen.push(result));
    expect(seen.map((result) => result.status)).toEqual(["changed"]);
    expect(platform.find("t-2")!["disabled"]).toBe(false);
  });

  const ELSEWHERE: [string, (platform: FakePlatform) => void][] = [
    [
      "another environment",
      (platform) => (platform.environment = `${ENV}-other`),
    ],
    ["another account", (platform) => (platform.person = "person-2")],
    ["no account", (platform) => (platform.person = null)],
    [
      "no environment",
      (platform) =>
        Object.defineProperty(platform, "environment", {
          get: () => {
            throw Error("Choose an environment.");
          },
        }),
    ],
  ];
  it.each(ELSEWHERE)(
    "sends nothing for a row's button in %s, and what it owes stays",
    async (_what, leave) => {
      const platform = seeded(OLD_ONE, SECOND);
      platform.starts.set("t-2", [requested("s-1")]);
      const intents = [stored()];
      const rows = rowsFor(platform, intents);
      const context = within(platform, intents);
      const results = await syncTriggers(rows, context);
      const sent = platform.sent.length;
      leave(platform);
      const elsewhere =
        "Studio did nothing to notify-team-2: the environment or the account changed since you opened this.";
      for (const answered of [
        await chooseResult(results[1]!, "stop", context),
        await retryResult({ ...results[1]!, status: "failed" }, context),
      ]) {
        expect(statuses([answered])).toEqual([["changed", elsewhere]]);
        expect(keep(intents, [results[0]!, answered], rows)).toMatchObject({
          resources: { "env-1": "t-1" },
          pendingStops: { "env-1": [{ triggerId: "t-2" }] },
        });
      }
      expect(platform.sent).toHaveLength(sent);
      // Keep it for now asks nothing of the platform.
      expect(await chooseResult(results[1]!, "keep", context)).toMatchObject({
        status: "kept",
      });
    },
  );
  it.each(ELSEWHERE)(
    "sends nothing more once the person is in %s while rows are set up",
    async (_what, leave) => {
      const platform = seeded(OLD_ONE, SECOND);
      const intents = [stored()];
      const rows = rowsFor(platform, intents);
      const context = within(platform, intents);
      const results = await syncTriggers(rows, context, () => leave(platform));
      expect(results.map((result) => result.status)).toEqual([
        "moved",
        "changed",
      ]);
      expect(platform.writes()).toEqual(["POST /event-triggers/t-1/retarget"]);
      expect(attention(results)).toBe("1 trigger needs attention.");
      // Both still react: the stop Studio didn't get to is owed.
      expect(keep(intents, results, rows)).toMatchObject({
        resources: { "env-1": "t-1" },
        pendingStops: {
          "env-1": [{ triggerId: "t-2", revision: 1, activationId: "a-older" }],
        },
      });
    },
  );
  it("goes on when the same account is read again with one more grant", async () => {
    const platform = seeded(OLD_ONE, SECOND);
    const intents = [stored()];
    const rows = rowsFor(platform, intents);
    const results = await syncTriggers(rows, within(platform, intents), () =>
      platform.grants.push("run.read"),
    );
    expect(results.map((result) => result.status)).toEqual([
      "moved",
      "stopped",
    ]);
  });
  it("owes no stop of a trigger left behind it didn't get to", async () => {
    const platform = seeded(OLD_ONE, {
      ...SECOND,
      events: ["transaction.completed"],
    });
    const intents = [stored()];
    const rows = rowsFor(platform, intents).map((row) =>
      row.kind === "left-behind" ? withStop(row, true) : row,
    );
    const results = await syncTriggers(rows, within(platform, intents), () => {
      platform.person = "person-2";
    });
    expect(results.map((result) => result.status)).toEqual([
      "moved",
      "changed",
    ]);
    expect(keep(intents, results, rows)).not.toHaveProperty("pendingStops");
  });

  it("owes the stop of a duplicate the person unticked, and drops the stops that no longer count", async () => {
    const platform = seeded(OLD_ONE, SECOND);
    const intents = [
      stored({
        pendingStops: {
          "env-1": [
            { triggerId: "t-gone", revision: 1, activationId: "a-older" },
          ],
          "env-2": [{ triggerId: "t-7", revision: 1, activationId: "a-9" }],
        },
      }),
    ];
    const rows = rowsFor(platform, intents).map((row) =>
      row.kind === "duplicate" ? withStop(row, false) : row,
    );
    const results = await syncTriggers(rows, within(platform, intents));
    expect(statuses(results)).toEqual([
      ["moved", "Moved notify-team to 1.2.0"],
      ["kept", "Kept notify-team-2 for now."],
    ]);
    expect(attention(results)).toBeNull();
    expect(keep(intents, results, rows)["pendingStops"]).toEqual({
      "env-1": [{ triggerId: "t-2", revision: 1, activationId: "a-older" }],
      "env-2": [{ triggerId: "t-7", revision: 1, activationId: "a-9" }],
    });
  });

  it("still owes a stop after activating a version that declares no platform event", async () => {
    const platform = seeded(SECOND);
    const pendingStops = {
      "env-1": [{ triggerId: "t-2", revision: 1, activationId: "a-older" }],
    };
    const intents: TriggerIntent[] = [{ ...EVENT, pendingStops }];
    const rows = rowsFor(platform, intents, null);
    expect(rows.map((row) => row.kind)).toEqual(["left-behind"]);
    const results = await syncTriggers(rows, within(platform, intents));
    expect(keep(intents, results, rows)["pendingStops"]).toEqual(pendingStops);
  });

  it("owes nothing for a trigger it moved, even one whose stop was owed", async () => {
    // Its stop was owed on this computer; the version now reacts as it does.
    const platform = seeded({ ...SECOND, revision: 2 });
    const intents: TriggerIntent[] = [
      {
        ...EVENT,
        pendingStops: {
          "env-1": [{ triggerId: "t-2", revision: 2, activationId: "a-older" }],
        },
      },
    ];
    const rows = rowsFor(platform, intents);
    const results = await syncTriggers(rows, within(platform, intents));
    expect(statuses(results)).toEqual([
      ["moved", "Moved notify-team-2 to 1.2.0"],
    ]);
    const kept = keep(intents, results, rows);
    expect(kept).toMatchObject({ resources: { "env-1": "t-2" } });
    expect(kept).not.toHaveProperty("pendingStops");
  });

  it("owes nothing once a duplicate is stopped, and never keeps it as this environment's", async () => {
    const platform = seeded(OLD_ONE, SECOND);
    const intents = [
      stored({
        pendingStops: {
          "env-1": [{ triggerId: "t-2", revision: 1, activationId: "a-older" }],
        },
      }),
    ];
    const rows = rowsFor(platform, intents);
    const results = await syncTriggers(rows, within(platform, intents));
    expect(statuses(results)).toEqual([
      ["moved", "Moved notify-team to 1.2.0"],
      ["stopped", "Stopped notify-team-2"],
    ]);
    const kept = keep(intents, results, rows);
    expect(kept).toMatchObject({ resources: { "env-1": "t-1" } });
    expect(kept).not.toHaveProperty("pendingStops");
  });

  it("counts a trigger changed by someone else as needing attention, apart from choices", async () => {
    const platform = seeded(OLD_ONE, SECOND);
    const intents = [stored()];
    const rows = rowsFor(platform, intents);
    platform.find("t-2")!["revision"] = 9;
    const results = await syncTriggers(rows, within(platform, intents));
    expect(results.map((result) => result.status)).toEqual([
      "moved",
      "changed",
    ]);
    expect(attention(results)).toBe("1 trigger needs attention.");
    const waiting = { ...results[1]!, status: "waiting" as const };
    expect(attention([results[1]!, waiting])).toBe(
      "1 trigger needs attention. 1 trigger needs your choice.",
    );
    expect(attention([waiting, waiting])).toBe("2 triggers need your choice.");
  });

  it("keeps what it owes when another trigger's answer is lost", async () => {
    const platform = seeded(OLD_ONE);
    platform.starts.set("t-1", [requested("s-1")]);
    const intents = [stored(), HOOK];
    const api: SyncContext["api"] = {
      environment: platform.environment,
      expectedPrincipal: platform.expectedPrincipal,
      page: () => platform.page(),
      request: (path, method) => platform.request(path, method),
      mutate: async <T>(
        path: string,
        method: string,
        body: unknown,
        revision?: number,
      ) => {
        if (path.endsWith("/triggers")) throw new TypeError("Failed to fetch");
        return platform.mutate<T>(path, method, body, revision);
      },
    };
    const rows = rowsFor(platform, [HOOK, ...intents.slice(0, 1)], WIDER);
    const results = await syncTriggers(rows, {
      ...within(platform, intents),
      api,
    });
    expect(statuses(results)).toEqual([
      [
        "failed",
        "Couldn't set up this trigger: Studio could not reach its local host. Check that Weave Studio is still running, then try again.",
      ],
      [
        "waiting",
        "Created notify-team-2. notify-team has 1 event waiting to start. Stopping it now skips them.",
      ],
    ]);
    expect(keep(intents, results, rows)).toHaveProperty("pendingStops");
    const again = await retryResult(results[0]!, {
      ...within(platform, intents),
      api,
    });
    expect(again.status).toBe("failed");
  });

  it("is set up beside other triggers, each row told as it finishes", async () => {
    const platform = seeded();
    const hook = view("w-new", "orders-in", 1, "a-new");
    const api: SyncContext["api"] = {
      environment: platform.environment,
      expectedPrincipal: platform.expectedPrincipal,
      page: () => platform.page(),
      request: (path, method) => platform.request(path, method),
      mutate: async <T>(
        path: string,
        method: string,
        body: unknown,
        revision?: number,
      ) =>
        path.endsWith("/triggers")
          ? (hook as T)
          : platform.mutate<T>(path, method, body, revision),
    };
    const rows = rowsFor(platform, [HOOK, EVENT]);
    const told: [number, string][] = [];
    await syncTriggers(
      rows,
      { ...within(platform, [HOOK, EVENT]), api },
      (index, result) => told.push([index, result.text]),
    );
    expect(told).toEqual([
      [0, "Created webhook orders-in"],
      [1, "Created notify-team"],
    ]);
  });
});
