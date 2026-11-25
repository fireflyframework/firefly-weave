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
// What the activation dialog says will happen to each trigger: create it,
// move it to the new version, or why it can't, plus the triggers this
// workflow runs that no trigger choice describes any more.
import { describe, expect, it } from "vitest";
import type {
  ResourceKind,
  ResourceList,
  TriggerResource,
} from "../src/app/editor/activation/trigger-resources-api";
import {
  triggerPlan,
  type PlanActivation,
  type TriggerPlanInput,
} from "../src/app/editor/activation/trigger-plan";
import { withStop } from "../src/app/editor/activation/trigger-plan";
import type { TriggerIntent } from "../src/app/editor/state/canvas-sidecar";

const INPUT = {
  type: "object",
  properties: { region: { type: "string" } },
  required: ["region"],
};
const OLD_INPUT = { type: "object", properties: { id: { type: "string" } } };
const ACTIVATIONS: PlanActivation[] = [
  { id: "a-1", workflow: "orders", version: "1.1.0", inputSchema: INPUT },
  { id: "a-old", workflow: "orders", version: "1.0.0", inputSchema: OLD_INPUT },
  { id: "x-1", workflow: "billing", version: "3.0.0" },
  { id: "a-unread", workflow: "orders", version: "1.1.5" },
];

function webhook(
  id: string,
  name: string,
  activationId: string,
  extra = {},
): TriggerResource<"webhook"> {
  const view = {
    id,
    name,
    kind: "run" as const,
    activation_id: activationId,
    secret_ref: "signing",
    payload_schema: INPUT,
    max_body_bytes: 1048576,
    tolerance_seconds: 300,
    principal_id: "p-1",
    disabled: false,
    revision: 2,
    ...extra,
  };
  return {
    kind: "webhook",
    id,
    name,
    activationId,
    revision: 2,
    disabled: view.disabled,
    view,
  };
}

const list = <K extends ResourceKind>(
  items: TriggerResource<K>[],
  truncated = false,
): ResourceList<K> => ({
  items,
  truncated,
});

function plan(
  intents: TriggerIntent[],
  resources: TriggerPlanInput["resources"] = {},
  extra: Partial<TriggerPlanInput> = {},
) {
  return triggerPlan({
    intents,
    resources: {
      webhook: list([]),
      schedule: list([]),
      broker: list([]),
      email: list([]),
      provider: list([]),
      ...resources,
    },
    activations: ACTIVATIONS,
    workflow: "orders",
    version: { version: "1.2.0", inputSchema: INPUT },
    environmentId: "local",
    ...extra,
  });
}

const HOOK: TriggerIntent = {
  id: "t-1",
  kind: "webhook",
  name: "orders-in",
  secretRef: "signing",
};

describe("webhooks", () => {
  it("creates a webhook that doesn't exist yet", () => {
    expect(plan([HOOK])).toEqual([
      {
        kind: "create",
        intentId: "t-1",
        resourceKind: "webhook",
        text: "Create webhook orders-in",
      },
    ]);
  });

  it("moves this workflow's webhook of the same name and keeps its URL", () => {
    const resource = webhook("w-1", "orders-in", "a-1");
    expect(plan([HOOK], { webhook: list([resource]) })).toEqual([
      {
        kind: "retarget",
        intentId: "t-1",
        resourceKind: "webhook",
        resource,
        from: "1.1.0",
        to: "1.2.0",
        text: "Move webhook orders-in from 1.1.0 to 1.2.0 (the URL stays the same)",
      },
    ]);
  });

  it("follows the resource the trigger choice stored for this environment before its name", () => {
    const renamed = webhook("w-9", "orders-renamed", "a-1");
    const named = webhook("w-1", "orders-in", "a-1");
    const rows = plan([{ ...HOOK, resources: { local: "w-9", prod: "w-1" } }], {
      webhook: list([named, renamed]),
    });
    expect(rows.map((row) => [row.kind, row.resource?.id, row.text])).toEqual([
      [
        "retarget",
        "w-9",
        "Move webhook orders-renamed from 1.1.0 to 1.2.0 (the URL stays the same)",
      ],
      ["left-behind", "w-1", "Webhook orders-in keeps running on 1.1.0"],
    ]);
    expect(rows[1].stop).toBe(false);
  });

  it("falls back to the name when the stored resource is gone, stopped or another workflow's", () => {
    const named = webhook("w-1", "orders-in", "a-1");
    for (const stored of [
      [],
      [webhook("w-9", "orders-in-2", "a-1", { disabled: true })],
      [webhook("w-9", "orders-in-2", "x-1")],
    ]) {
      const rows = plan([{ ...HOOK, resources: { local: "w-9" } }], {
        webhook: list([named, ...stored]),
      });
      expect(rows.map((row) => [row.kind, row.resource?.id])).toEqual([
        ["retarget", "w-1"],
      ]);
    }
  });

  it("never matches another workflow's webhook, even with the same name", () => {
    const rows = plan([HOOK], {
      webhook: list([webhook("w-2", "orders-in", "x-1")]),
    });
    expect(rows.map((row) => row.text)).toEqual(["Create webhook orders-in"]);
  });

  it("blocks the move when the payload changed and offers a new webhook", () => {
    const resource = webhook("w-1", "orders-in", "a-old", {
      payload_schema: OLD_INPUT,
    });
    expect(plan([HOOK], { webhook: list([resource]) })).toEqual([
      {
        kind: "blocked",
        intentId: "t-1",
        resourceKind: "webhook",
        resource,
        text: "The webhook's payload changed. Create a new trigger for the changed input.",
      },
    ]);
  });

  it("moves it when the payload differs but the input it starts with didn't change", () => {
    const resource = webhook("w-1", "orders-in", "a-1", { payload_schema: {} });
    expect(plan([HOOK], { webhook: list([resource]) })[0].kind).toBe(
      "retarget",
    );
  });

  it("proposes the move when it can't tell, and leaves the decision to the platform", () => {
    const resource = webhook("w-1", "orders-in", "a-unread", {
      payload_schema: OLD_INPUT,
    });
    expect(
      plan([HOOK], { webhook: list([resource]) }).map((row) => [
        row.kind,
        row.text,
      ]),
    ).toEqual([
      [
        "retarget",
        "Move webhook orders-in from 1.1.5 to 1.2.0 (the URL stays the same)",
      ],
    ]);
  });

  it("names this workflow's running webhooks no trigger choice describes, and leaves stopped ones out", () => {
    const rows = plan([], {
      webhook: list([
        webhook("w-3", "orders-old", "a-1"),
        webhook("w-4", "orders-stopped", "a-1", { disabled: true }),
        webhook("w-5", "billing-in", "x-1"),
      ]),
    });
    expect(rows).toEqual([
      {
        kind: "left-behind",
        resourceKind: "webhook",
        resource: expect.objectContaining({ id: "w-3" }),
        from: "1.1.0",
        stop: false,
        text: "Webhook orders-old keeps running on 1.1.0",
      },
    ]);
  });
});

describe("schedules", () => {
  const schedule = (
    id: string,
    cron: string,
    activationId: string,
    status = "enabled",
  ): TriggerResource<"schedule"> => ({
    kind: "schedule",
    id,
    name: cron,
    activationId,
    revision: 3,
    disabled: status !== "enabled",
    view: {
      id,
      cron,
      timezone: "UTC",
      missed_policy: "skip",
      activation_id: activationId,
      input: { region: "eu" },
      revision: 3,
      principal_id: "p-1",
      status: status as "enabled",
      next_due_at: "2026-11-23T09:00:00Z",
      blocked_reason: null,
    },
  });
  const MONDAYS: TriggerIntent = {
    id: "t-2",
    kind: "schedule",
    cron: "0 9 * * 1",
    input: { region: "eu" },
  };

  it("creates a schedule the trigger choice hasn't made here yet", () => {
    expect(plan([MONDAYS])).toEqual([
      {
        kind: "create",
        intentId: "t-2",
        resourceKind: "schedule",
        text: "Create schedule 'Mondays 09:00 UTC'",
      },
    ]);
  });

  it("moves the schedule it made here, named by its calendar now", () => {
    const resource = schedule("s-1", "0 8 * * 1", "a-1");
    const rows = plan([{ ...MONDAYS, resources: { local: "s-1" } }], {
      schedule: list([resource]),
    });
    expect(rows).toEqual([
      {
        kind: "schedule-move",
        intentId: "t-2",
        resourceKind: "schedule",
        resource,
        from: "1.1.0",
        to: "1.2.0",
        text: "Move schedule 'Mondays 09:00 UTC' from 1.1.0 to 1.2.0",
      },
    ]);
  });

  it("stops the move when the run input no longer fits the new version", () => {
    const resource = schedule("s-1", "0 9 * * 1", "a-1");
    const rows = plan(
      [{ ...MONDAYS, input: {}, resources: { local: "s-1" } }],
      { schedule: list([resource]) },
    );
    expect(rows.map((row) => [row.kind, row.text])).toEqual([
      [
        "schedule-input",
        "The schedule's run input no longer fits this version. Update it in the trigger's step details.",
      ],
    ]);
    expect(plan([{ ...MONDAYS, input: {} }])[0].kind).toBe("schedule-input");
  });

  it("names a schedule left behind by its calendar", () => {
    expect(
      plan([], { schedule: list([schedule("s-1", "0 9 * * 1", "a-1")]) })[0]
        .text,
    ).toBe("Schedule 'Mondays 09:00 UTC' keeps running on 1.1.0");
  });
});

describe("Kafka, email and app events", () => {
  const kafka = (
    name: string,
    activationId: string,
  ): TriggerResource<"broker"> => ({
    kind: "broker",
    id: `k-${name}`,
    name,
    activationId,
    revision: 1,
    disabled: false,
    view: {
      id: `k-${name}`,
      name,
      driver: "kafka",
      connection_revision_id: "c-1",
      cluster_id: "main",
      topic: "orders.created",
      kind: "run",
      activation_id: activationId,
      payload_schema: INPUT,
      dead_letter_policy: "halt",
      binding_id: "b-1",
      principal_id: "p-1",
      disabled: false,
      blocked: false,
      revision: 1,
    },
  });
  const mail = (
    connection: string,
    activationId: string,
  ): TriggerResource<"email"> => ({
    kind: "email",
    id: `m-${connection}`,
    name: "INBOX",
    activationId,
    revision: 1,
    disabled: false,
    view: {
      id: `m-${connection}`,
      connection_revision_id: connection,
      activation_id: activationId,
      mailbox: "INBOX",
      revision: 1,
      disabled: false,
    },
  });
  const KAFKA: TriggerIntent = { id: "t-3", kind: "broker", name: "orders" };
  const EMAIL: TriggerIntent = { id: "t-4", kind: "email" };
  const APP: TriggerIntent = {
    id: "t-5",
    kind: "provider",
    name: "support",
    provider: "telegram",
    package: "weave-telegram",
    packageVersion: "1.0.0",
    schemaDigest: "a".repeat(64),
  };
  const chosen = {
    emailConnection: {
      revisionId: "c-mail",
      name: "mail-dev",
      mailbox: "INBOX",
    },
  };

  it("says what it creates", () => {
    expect(
      plan([KAFKA, EMAIL, APP], {}, chosen).map((row) => row.text),
    ).toEqual([
      "Create Kafka trigger orders",
      "Start reading INBOX on mail-dev",
      "Create app event source support",
    ]);
    expect(plan([EMAIL])[0].text).toBe(
      "Start reading mail on the email connection you choose for it",
    );
  });

  it("sends an app event source Studio can't describe to the CLI, and creates nothing", () => {
    expect(
      plan([
        { id: "t-6", kind: "provider", name: "support", provider: "telegram" },
      ]),
    ).toEqual([
      {
        kind: "cli",
        intentId: "t-6",
        resourceKind: "provider",
        text: "Create the app event source support with the CLI. Studio can't read app event schemas yet.",
      },
    ]);
  });

  it("moves a Kafka trigger by name and an email source by its connection", () => {
    const rows = plan(
      [KAFKA, EMAIL],
      {
        broker: list([kafka("orders", "a-1")]),
        email: list([mail("c-other", "a-1"), mail("c-mail", "a-1")]),
      },
      chosen,
    );
    expect(rows.map((row) => [row.kind, row.resource?.id, row.text])).toEqual([
      ["retarget", "k-orders", "Move Kafka trigger orders from 1.1.0 to 1.2.0"],
      ["retarget", "m-c-mail", "Move email source INBOX from 1.1.0 to 1.2.0"],
      ["left-behind", "m-c-other", "Email source INBOX keeps running on 1.1.0"],
    ]);
  });
});

describe("other trigger choices and long lists", () => {
  it("says a called workflow creates nothing and a manual form says nothing", () => {
    expect(
      plan([
        { id: "t-6", kind: "called" },
        { id: "t-7", kind: "manual" },
      ]),
    ).toEqual([
      {
        kind: "called",
        intentId: "t-6",
        text: "Callers pin this version when they activate",
      },
    ]);
  });

  it("sends people to the CLI once when a list is too long to check, and plans the kinds it could check", () => {
    const rows = plan([HOOK, { id: "t-3", kind: "broker", name: "orders" }], {
      webhook: list([webhook("w-1", "orders-in", "a-1")], true),
      schedule: list([], true),
    });
    expect(rows).toEqual([
      {
        kind: "create",
        intentId: "t-3",
        resourceKind: "broker",
        text: "Create Kafka trigger orders",
      },
      {
        kind: "truncated",
        text: "Too many triggers to check here. Use the CLI.",
      },
    ]);
  });
});

describe("the workflow's platform event trigger", () => {
  const EVENT: TriggerIntent = { id: "t-8", kind: "event" };
  const ATTENTION = {
    events: ["transaction.in_doubt" as const],
    from: ["move-between-systems"],
  };
  const trigger = (
    id: string,
    name: string,
    activationId: string,
    extra: Partial<TriggerResource<"event">["view"]> = {},
  ): TriggerResource<"event"> => {
    const view = {
      id,
      name,
      activation_id: activationId,
      events: ["transaction.in_doubt" as const],
      source_workflows: ["move-between-systems"],
      may_reconcile: false,
      workflow_name: "orders",
      principal_id: "p-1",
      revision: 2,
      disabled: false,
      created_at: "2026-11-20T09:00:00Z",
      ...extra,
    };
    return {
      kind: "event",
      id,
      name,
      activationId,
      revision: view.revision,
      disabled: view.disabled,
      view,
    };
  };
  const events = (...items: TriggerResource<"event">[]) => ({
    event: list(items),
  });

  it("is created in its place among the trigger choices", () => {
    const rows = plan([EVENT, HOOK], events(), { declaration: ATTENTION });
    expect(rows.map((row) => [row.kind, row.intentId, row.text])).toEqual([
      [
        "create",
        "t-8",
        "Create platform event trigger orders for Needs attention · from move-between-systems",
      ],
      ["create", "t-1", "Create webhook orders-in"],
    ]);
    expect(rows[0]).toMatchObject({
      resourceKind: "event",
      event: { kind: "create", name: "orders" },
    });
  });

  it("moves the trigger stored for this environment, and leaves another behind after the rest", () => {
    const rows = plan(
      [{ ...EVENT, resources: { local: "e-2", prod: "e-1" } }, HOOK],
      events(
        trigger("e-1", "orders", "a-old", {
          events: ["transaction.completed"],
        }),
        trigger("e-2", "orders-2", "a-1"),
      ),
      { declaration: ATTENTION },
    );
    expect(rows.map((row) => [row.kind, row.event?.kind, row.text])).toEqual([
      [
        "retarget",
        "retarget",
        "Move platform event trigger orders-2 from 1.1.0 to 1.2.0. Runs start as you after the move, including events waiting to start.",
      ],
      ["create", undefined, "Create webhook orders-in"],
      [
        "left-behind",
        "left-behind",
        "Platform event trigger orders keeps running on 1.0.0",
      ],
    ]);
    expect(rows.map((row) => [row.from, row.to, row.stop])).toEqual([
      ["1.1.0", "1.2.0", undefined],
      [undefined, undefined, undefined],
      ["1.0.0", undefined, false],
    ]);
  });

  it("replaces it when the version reacts to other events", () => {
    const rows = plan(
      [{ ...EVENT, resources: { local: "e-1" } }],
      events(trigger("e-1", "orders", "a-1")),
      {
        declaration: {
          events: ["transaction.compensated", "transaction.in_doubt"],
          from: null,
        },
      },
    );
    expect(rows).toMatchObject([
      {
        kind: "replace",
        intentId: "t-8",
        from: "1.1.0",
        to: "1.2.0",
        event: { name: "orders-2", binding: { id: "e-1" } },
      },
    ]);
  });

  it("shows a stop owed in this environment as a duplicate, on, last", () => {
    const owed = trigger("e-1", "orders", "a-old", {
      events: ["transaction.completed"],
    });
    const rows = plan(
      [
        {
          ...EVENT,
          resources: { local: "e-2" },
          pendingStops: {
            local: [{ triggerId: "e-1", revision: 2, activationId: "a-old" }],
          },
        },
      ],
      events(owed, trigger("e-2", "orders-2", "a-1")),
      { declaration: ATTENTION },
    );
    expect(rows.map((row) => [row.kind, row.stop])).toEqual([
      ["retarget", undefined],
      ["duplicate", true],
    ]);
    // Owed in another environment, it is a trigger left behind here.
    const elsewhere = plan(
      [
        {
          ...EVENT,
          resources: { local: "e-2" },
          pendingStops: {
            prod: [{ triggerId: "e-1", revision: 2, activationId: "a-old" }],
          },
        },
      ],
      events(owed, trigger("e-2", "orders-2", "a-1")),
      { declaration: ATTENTION },
    );
    expect(elsewhere.map((row) => row.kind)).toEqual([
      "retarget",
      "left-behind",
    ]);
  });

  it("follows the version being activated, not the draft's trigger choices", () => {
    // Removed from the draft since it was published: still declared, so still set up.
    expect(
      plan([HOOK], events(), { declaration: ATTENTION }).map((row) => [
        row.kind,
        row.intentId,
        row.resourceKind,
      ]),
    ).toEqual([
      ["create", "t-1", "webhook"],
      ["create", undefined, "event"],
    ]);
    // Added to the draft since: this version creates none, and leaves what runs.
    expect(
      plan([EVENT, HOOK], events(trigger("e-1", "orders", "a-1")), {
        declaration: null,
      }).map((row) => [row.kind, row.stop]),
    ).toEqual([
      ["create", undefined],
      ["left-behind", false],
    ]);
  });

  it("plans nothing for it while its list is too long or wasn't read", () => {
    const declaration = ATTENTION;
    expect(
      plan(
        [EVENT],
        { event: list([trigger("e-1", "orders", "a-1")], true) },
        { declaration },
      ).map((row) => row.kind),
    ).toEqual(["truncated"]);
    expect(plan([EVENT], {}, { declaration })).toEqual([]);
  });

  it("keeps the person's Stop it in the row that is applied", () => {
    const [left] = plan([], events(trigger("e-1", "orders", "a-1")));
    expect(left).toMatchObject({ stop: false, event: { stop: false } });
    expect(withStop(left, true)).toMatchObject({
      stop: true,
      event: { stop: true },
    });
    const [hook] = plan([], {
      webhook: list([webhook("w-9", "orders-old", "a-1")]),
    });
    expect(withStop(hook, true)).toEqual({ ...hook, stop: true });
  });
});
