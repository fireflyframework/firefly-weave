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
// The trigger resources client: list, read, create and move webhooks,
// schedules, Kafka triggers, email sources and app event sources.
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import {
  createResource,
  listTriggerResources,
  moveSchedule,
  readResource,
  retarget,
  type TriggerApi,
} from "../src/app/editor/activation/trigger-resources-api";

const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";

interface Call {
  method: string;
  path: string;
  body?: unknown;
  revision?: number;
}

class FakeApi implements TriggerApi {
  readonly environment = ENV;
  readonly calls: Call[] = [];
  pages: Record<string, unknown>[][] = [];
  answers: unknown[] = [];
  async page(collection: string, environment = false, cursor?: string) {
    this.calls.push({
      method: "GET",
      path: `${collection}?cursor=${cursor ?? ""}`,
    });
    expect(environment).toBe(true);
    const index = cursor === undefined ? 0 : Number(cursor);
    return {
      items: this.pages[index] ?? [],
      next_cursor: index + 1 < this.pages.length ? String(index + 1) : null,
    };
  }
  async request<T>(path: string, method = "GET", body?: unknown) {
    this.calls.push({ method, path, body });
    return this.next() as T;
  }
  async mutate<T>(
    path: string,
    method: string,
    body: unknown,
    revision?: number,
  ) {
    this.calls.push({ method, path, body, revision });
    return this.next() as T;
  }
  private next() {
    const answer = this.answers.shift();
    if (answer instanceof Error) throw answer;
    return answer;
  }
}

const WEBHOOK = {
  id: "w-1",
  name: "orders-in",
  kind: "run",
  activation_id: "a-1",
  secret_ref: "signing",
  payload_schema: {},
  max_body_bytes: 1048576,
  tolerance_seconds: 300,
  principal_id: "p-1",
  disabled: false,
  revision: 3,
};

describe("listing trigger resources", () => {
  it("pages each kind's collection and reads activation, revision and state", async () => {
    const api = new FakeApi();
    api.pages = [
      [WEBHOOK],
      [{ ...WEBHOOK, id: "w-2", disabled: true, revision: 1 }],
    ];
    const listed = await listTriggerResources(api, "webhook");
    expect(listed.truncated).toBe(false);
    expect(
      listed.items.map((item) => [
        item.id,
        item.name,
        item.activationId,
        item.revision,
        item.disabled,
      ]),
    ).toEqual([
      ["w-1", "orders-in", "a-1", 3, false],
      ["w-2", "orders-in", "a-1", 1, true],
    ]);
    expect(api.calls.map((call) => call.path)).toEqual([
      "triggers?cursor=",
      "triggers?cursor=1",
    ]);
  });

  it.each([
    ["schedule", "schedules"],
    ["broker", "broker-triggers"],
    ["email", "email/sources"],
    ["provider", "provider-sources"],
  ] as const)("lists %s resources from %s", async (kind, collection) => {
    const api = new FakeApi();
    await listTriggerResources(api, kind);
    expect(api.calls[0].path).toBe(`${collection}?cursor=`);
  });

  it("stops after ten pages and says the list is truncated", async () => {
    const api = new FakeApi();
    api.pages = Array.from({ length: 12 }, (_, page) => [
      { ...WEBHOOK, id: `w-${page}` },
    ]);
    const listed = await listTriggerResources(api, "webhook");
    expect(listed.items).toHaveLength(10);
    expect(listed.truncated).toBe(true);
    expect(api.calls).toHaveLength(10);
  });

  it("names an email source by its mailbox and a schedule by its calendar", async () => {
    const api = new FakeApi();
    api.pages = [
      [
        {
          id: "m-1",
          connection_revision_id: "c-1",
          activation_id: null,
          mailbox: "INBOX",
          revision: 2,
          disabled: true,
        },
      ],
    ];
    const [mail] = (await listTriggerResources(api, "email")).items;
    expect([mail.name, mail.activationId, mail.disabled]).toEqual([
      "INBOX",
      null,
      true,
    ]);
    const schedules = new FakeApi();
    schedules.pages = [
      [
        {
          id: "s-1",
          cron: "0 9 * * 1",
          activation_id: "a-1",
          revision: 4,
          status: "disabled",
        },
      ],
    ];
    const [schedule] = (await listTriggerResources(schedules, "schedule"))
      .items;
    expect([schedule.name, schedule.revision, schedule.disabled]).toEqual([
      "0 9 * * 1",
      4,
      true,
    ]);
  });
});

describe("reading one resource", () => {
  it("answers the view with its revision", async () => {
    const api = new FakeApi();
    api.answers = [WEBHOOK];
    const read = await readResource(api, "webhook", "w/1");
    expect([read.id, read.revision, read.view]).toEqual(["w-1", 3, WEBHOOK]);
    expect(api.calls[0]).toEqual({
      method: "GET",
      path: `${ENV}/triggers/w%2F1`,
      body: undefined,
    });
  });
});

describe("moving a trigger to another activation", () => {
  it("sends the revision it read as If-Match and answers the moved resource", async () => {
    const api = new FakeApi();
    api.answers = [{ ...WEBHOOK, activation_id: "a-2", revision: 4 }];
    const moved = await retarget(api, "broker", "k-1", "a-2", 3);
    expect(moved).toMatchObject({
      ok: true,
      resource: { activationId: "a-2", revision: 4 },
    });
    expect(api.calls[0]).toEqual({
      method: "POST",
      path: `${ENV}/broker-triggers/k-1/retarget`,
      body: { activation_id: "a-2" },
      revision: 3,
    });
  });

  it.each([
    [409, "WV-REVISION", "changed"],
    [409, "WV-TRIGGER-SCHEMA-CHANGED", "schema-changed"],
    [409, "WV-TRIGGER-DISABLED", "stopped"],
    [409, "WV-TRIGGER-WORKFLOW", "other-workflow"],
    [409, "WV-TRIGGER-KIND", "not-run"],
    [422, "WV-ETAG", "no-revision"],
    [403, "WV-FORBIDDEN", "forbidden"],
    [404, "WV-NOT-FOUND", "missing"],
    [500, "WV-INTERNAL", "failed"],
  ] as const)(
    "maps %i %s to a %s refusal with the platform's message",
    async (status, code, kind) => {
      const api = new FakeApi();
      api.answers = [
        new ApiError(status, {
          code,
          message: "Create a new trigger for the changed input",
        }),
      ];
      const refused = await retarget(api, "webhook", "w-1", "a-2", 3);
      expect(refused).toEqual({
        ok: false,
        refusal: { kind, status, code, message: expect.any(String) },
      });
    },
  );

  it("moves a schedule by saving it with the revision it read", async () => {
    const api = new FakeApi();
    const schedule = {
      id: "s-1",
      cron: "0 9 * * 1",
      timezone: "UTC" as const,
      missed_policy: "skip" as const,
      activation_id: "a-1",
      input: { region: "eu" },
      revision: 4,
      principal_id: "p-1",
      status: "enabled" as const,
      next_due_at: "2026-11-23T09:00:00Z",
      blocked_reason: null,
    };
    api.answers = [{ ...schedule, activation_id: "a-2", revision: 5 }];
    const moved = await moveSchedule(api, schedule, "a-2");
    expect(moved).toMatchObject({
      ok: true,
      resource: { activationId: "a-2", revision: 5 },
    });
    expect(api.calls[0]).toEqual({
      method: "POST",
      path: `${ENV}/schedules`,
      body: {
        id: "s-1",
        cron: "0 9 * * 1",
        timezone: "UTC",
        missed_policy: "skip",
        activation_id: "a-2",
        input: { region: "eu" },
      },
      revision: 4,
    });
    api.answers = [
      new ApiError(409, {
        code: "WV-SCHEDULE-REVISION",
        message: "Schedule revision changed",
      }),
    ];
    expect(
      await moveSchedule(api, schedule, "a-3", { region: "us" }),
    ).toMatchObject({ ok: false, refusal: { kind: "changed" } });
    expect(api.calls[1].body).toMatchObject({
      activation_id: "a-3",
      input: { region: "us" },
    });
  });

  it("lets other failures through", async () => {
    const api = new FakeApi();
    api.answers = [new TypeError("offline")];
    await expect(retarget(api, "email", "m-1", "a-2", 1)).rejects.toThrow(
      "offline",
    );
  });
});

describe("creating a resource from a trigger choice", () => {
  const target = { activationId: "a-2", inputSchema: { type: "object" } };

  it("creates a webhook whose payload schema is the workflow's input", async () => {
    const api = new FakeApi();
    api.answers = [WEBHOOK];
    const created = await createResource(
      api,
      {
        id: "t-1",
        kind: "webhook",
        name: "orders-in",
        secretRef: "signing",
        maxBodyBytes: 65536,
      },
      target,
    );
    expect(created.ok).toBe(true);
    expect(api.calls[0]).toMatchObject({
      method: "POST",
      path: `${ENV}/triggers`,
      body: {
        name: "orders-in",
        kind: "run",
        activation_id: "a-2",
        secret_ref: "signing",
        payload_schema: { type: "object" },
        max_body_bytes: 65536,
        tolerance_seconds: 300,
      },
    });
  });

  it("creates a Kafka trigger on the chosen connection", async () => {
    const api = new FakeApi();
    api.answers = [{ ...WEBHOOK, id: "k-1" }];
    await createResource(
      api,
      {
        id: "t-2",
        kind: "broker",
        name: "orders",
        clusterId: "main",
        topic: "orders.created",
        deadLetterPolicy: "halt",
      },
      { ...target, connectionRevisionId: "c-1" },
    );
    expect(api.calls[0]).toMatchObject({
      path: `${ENV}/broker-triggers`,
      body: {
        name: "orders",
        driver: "kafka",
        connection_revision_id: "c-1",
        cluster_id: "main",
        topic: "orders.created",
        kind: "run",
        activation_id: "a-2",
        payload_schema: { type: "object" },
        dead_letter_policy: "halt",
      },
    });
  });

  it("creates an email source on the chosen connection and reads it back", async () => {
    const api = new FakeApi();
    api.answers = [
      { id: "m-1" },
      {
        id: "m-1",
        connection_revision_id: "c-2",
        activation_id: "a-2",
        mailbox: "INBOX",
        revision: 1,
        disabled: false,
      },
    ];
    const created = await createResource(
      api,
      { id: "t-3", kind: "email" },
      { ...target, connectionRevisionId: "c-2" },
    );
    expect(created).toMatchObject({
      ok: true,
      resource: { id: "m-1", name: "INBOX" },
    });
    expect(
      api.calls.map((call) => [call.method, call.path, call.body]),
    ).toEqual([
      [
        "POST",
        `${ENV}/email/sources`,
        { connection_revision_id: "c-2", activation_id: "a-2" },
      ],
      ["GET", `${ENV}/email/sources/m-1`, undefined],
    ]);
  });

  it("creates an app event source with its provider, package, schema digest, policy and mapping", async () => {
    const api = new FakeApi();
    api.answers = [{ ...WEBHOOK, id: "s-1" }];
    const mapping = { ref: "/payload/text" };
    await createResource(
      api,
      {
        id: "t-4",
        kind: "provider",
        name: "support",
        provider: "telegram",
        package: "weave-telegram",
        packageVersion: "1.0.0",
        schemaDigest: "a".repeat(64),
        policy: { account_id: "bot" },
        mapping,
      },
      { ...target, connectionRevisionId: "c-3" },
    );
    expect(api.calls[0]).toMatchObject({
      path: `${ENV}/provider-sources`,
      body: {
        name: "support",
        provider: "telegram",
        package: "weave-telegram",
        package_version: "1.0.0",
        schema_digest: "a".repeat(64),
        connection_revision_id: "c-3",
        policy: { account_id: "bot" },
        kind: "run",
        activation_id: "a-2",
        mapping,
      },
    });
  });

  it("saves a schedule with its calendar and run input", async () => {
    const api = new FakeApi();
    api.answers = [
      {
        id: "s-9",
        cron: "0 9 * * 1",
        activation_id: "a-2",
        revision: 1,
        status: "enabled",
      },
    ];
    await createResource(
      api,
      {
        id: "t-5",
        kind: "schedule",
        cron: "0 9 * * 1",
        input: { region: "eu" },
      },
      target,
    );
    expect(api.calls[0]).toMatchObject({
      path: `${ENV}/schedules`,
      body: {
        cron: "0 9 * * 1",
        timezone: "UTC",
        missed_policy: "skip",
        activation_id: "a-2",
        input: { region: "eu" },
      },
    });
  });

  it("refuses a choice that is still to be set up, without a request", async () => {
    const api = new FakeApi();
    const refused = await createResource(
      api,
      { id: "t-6", kind: "webhook", name: "orders-in" },
      target,
    );
    expect(refused).toEqual({
      ok: false,
      refusal: {
        kind: "incomplete",
        status: 0,
        code: "",
        message: "Choose the webhook's secret before activating.",
      },
    });
    const broker = await createResource(
      api,
      {
        id: "t-7",
        kind: "broker",
        name: "orders",
        clusterId: "main",
        topic: "t",
        deadLetterPolicy: "halt",
      },
      target,
    );
    expect(broker).toMatchObject({
      ok: false,
      refusal: { message: "Choose a Kafka connection before activating." },
    });
    expect(api.calls).toEqual([]);
  });
});
