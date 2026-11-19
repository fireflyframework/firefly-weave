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
// Alert records reach the screen only through their typed readers: a record
// with a field of the wrong shape is dropped rather than half shown, a
// recipient is a member or an address and never both, and every change
// carries its idempotency key or the revision it changes.
import { describe, expect, it } from "vitest";
import {
  acknowledgeAlert,
  countAlertWebhooks,
  createRule,
  listAlerts,
  readAlertPage,
  readChannel,
  readRule,
  saveAlertSettings,
  saveMyAlertAddress,
  updateRule,
  type AlertsTransport,
} from "../src/app/operate/alerts/alerts-api";
import * as fixture from "./alerts-fixture";

const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";
interface Sent {
  path: string;
  method: string;
  body: unknown;
  headers: Record<string, string>;
}
function transport(answer: (sent: Sent) => unknown) {
  const sent: Sent[] = [];
  const api: AlertsTransport = {
    environment: ENV,
    project: "/studio/api/api/v1/tenants/t/projects/p",
    async request<T>(
      path: string,
      method = "GET",
      body?: unknown,
      headers: Record<string, string> = {},
    ) {
      const request = { path, method, body, headers };
      sent.push(request);
      return answer(request) as T;
    },
  };
  return { api, sent };
}

describe("typed readers", () => {
  it("reads every fixture rule, an unknown signal included", () => {
    const read = fixture.rules().map(readRule);
    expect(read.every(Boolean)).toBe(true);
    expect(read[7]).toMatchObject({
      signal: "transactions.in_doubt",
      state: { state: "unsupported", unit: null, value: null },
    });
    expect(read[1]).toMatchObject({
      threshold: 0.1,
      selector: {},
      preset_id: null,
      state: { state: "firing", alert_id: fixture.ids.firingRate },
    });
  });
  it("drops a rule whose state or threshold has the wrong shape", () => {
    const [first] = fixture.rules();
    expect(
      readRule({ ...first, state: { ...first.state, state: "on" } }),
    ).toBeNull();
    expect(readRule({ ...first, threshold: "0" })).toBeNull();
    expect(readRule({ ...first, comparator: "eq" })).toBeNull();
    expect(readRule({ ...first, selector: { workflow: 3 } })).toBeNull();
  });
  it("reads a recipient as a member or an address, never both", () => {
    const [, team] = fixture.channels();
    expect(readChannel(team)?.recipients).toEqual([
      { member: fixture.BEN },
      { member: fixture.ME },
      { member: fixture.ANA },
      { address: "oncall@vendor.example" },
    ]);
    expect(
      readChannel({
        ...team,
        recipients: [{ member: fixture.BEN, address: "x@vendor.example" }],
      }),
    ).toBeNull();
  });
  it("treats an answer that isn't an alert page as a failed read", () => {
    expect(() => readAlertPage({ items: [], next_cursor: null })).toThrow(
      "Studio couldn't read the platform's answer.",
    );
    const page = readAlertPage(
      fixture.alertPage([...fixture.firing(), { id: "broken" }]),
    );
    expect(page.items.map((alert) => alert.id)).toEqual([
      fixture.ids.firingRate,
      fixture.ids.firingRunner,
    ]);
    expect([page.firing, page.critical]).toEqual([2, 1]);
  });
});

describe("requests", () => {
  it("asks for firing alerts with the state and limit in the query", async () => {
    const { api, sent } = transport(() => fixture.alertPage([], 0, 0));
    await listAlerts(api, { state: "firing", limit: 1 });
    expect(sent[0].path).toBe(`${ENV}/alerts?state=firing&limit=1`);
  });
  it("creates a rule with an idempotency key and updates it with its revision", async () => {
    const [first] = fixture.rules();
    const { api, sent } = transport(() => first);
    const draft = {
      name: "x",
      enabled: true,
      signal: "health.degraded",
      selector: {},
      comparator: "gt" as const,
      threshold: 0,
      window_seconds: null,
      for_seconds: 0,
      min_samples: 1,
      severity: "info" as const,
      channel_ids: [],
      renotify_seconds: 0,
    };
    await createRule(api, draft, "key-1");
    await updateRule(api, fixture.ids.degraded, 4, draft);
    expect(sent.map((request) => [request.method, request.path])).toEqual([
      ["POST", `${ENV}/alert-rules`],
      ["PUT", `${ENV}/alert-rules/${fixture.ids.degraded}`],
    ]);
    expect(sent[0].headers["Idempotency-Key"]).toBe("key-1");
    expect(sent[1].headers["If-Match"]).toBe('"4"');
    expect(sent[1].body).toEqual(draft);
  });
  it("saves settings never saved before without If-Match", async () => {
    const { api, sent } = transport(() => fixture.settings());
    const update = { recipient_domains: [], approval_channel_ids: [] };
    await saveAlertSettings(api, update, 0);
    await saveAlertSettings(api, update, 3);
    expect(sent[0].headers["If-Match"]).toBeUndefined();
    expect(sent[1].headers["If-Match"]).toBe('"3"');
  });
  it("acknowledges an alert and saves only the caller's own address", async () => {
    const { api, sent } = transport((request) =>
      request.path.endsWith("/acknowledge")
        ? fixture.firing()[0]
        : fixture.myAddress(),
    );
    await acknowledgeAlert(api, fixture.ids.firingRate);
    await saveMyAlertAddress(api, null);
    expect(sent.map((request) => [request.method, request.path])).toEqual([
      ["POST", `${ENV}/alerts/${fixture.ids.firingRate}/acknowledge`],
      ["PUT", "/studio/api/api/v1/tenants/t/my-alert-address"],
    ]);
    expect(sent[1].body).toEqual({ address: null });
  });
  it("counts only active subscriptions that receive alert changes", async () => {
    const { api, sent } = transport(() => fixture.subscriptions());
    expect(await countAlertWebhooks(api)).toBe(2);
    expect(sent[0].path).toBe(`${ENV}/subscriptions?limit=100`);
  });
});
