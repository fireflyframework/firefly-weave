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
// A mocked platform for the alert and administration screens: the fixture's
// alerts, rules, channels and settings at 10:54 in the local environment,
// the signed-in person as a UUID principal, every request recorded, and any
// answer replaceable by a status or another body.
import type { Page, Request, Route } from "@playwright/test";
import * as fixture from "../alerts-fixture";
import { connected } from "./support";

const TENANT = "/studio/api/api/v1/tenants/tenant";
const PROJECT = `${TENANT}/projects/project`;
export const ENV = `${PROJECT}/environments/development`;
/** An alert manager who reads the environment's status too. */
export const manager = [
  "alert.read",
  "alert.manage",
  "status.read",
  "email.send",
  "deployment.read",
  "run.read",
  "catalog.read",
];
/** Platform administrators: everything Settings › Administration asks for. */
export const administrator = [
  ...manager,
  "environment.manage",
  "compatibility.check",
  "retention.plan",
  "retention.apply",
  "grant.manage",
];
export interface Sent {
  method: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
}
/** A status (with a problem body) or a value; a function answers per request. */
export type Answer =
  | number
  | { status: number; json: unknown }
  | ((request: Request) => unknown)
  | object;
const problems: Record<number, { code: string; message: string }> = {
  403: { code: "WV-FORBIDDEN", message: "Forbidden" },
  503: {
    code: "WV-UNAVAILABLE",
    message: "The platform didn't answer in time.",
  },
  501: { code: "WV-UNAVAILABLE", message: "Not served" },
};

export interface AlertsPlatform {
  sent: Sent[];
  /** Answers by operation key: "rules", "firing", "resolved", "channels"… */
  answers: Record<string, Answer>;
}

/** The operation a request is, by method and path; "" for any other. */
function operation(method: string, path: string, query: URLSearchParams) {
  const env = path.startsWith(ENV) ? path.slice(ENV.length) : null;
  if (path === `${TENANT}/my-alert-address`)
    return method === "GET" ? "address" : "saveAddress";
  if (path === `${TENANT}/members`) return "members";
  if (path.startsWith(`${PROJECT}/operations/`)) {
    const rest = path.slice(`${PROJECT}/operations/`.length);
    if (rest === "compatibility") return "compatibility";
    if (rest === "compatibility/check") return "check";
    if (rest === "telemetry") return "telemetry";
    if (rest === "retention/plans") return "plan";
    if (/^retention\/plans\/[^/]+\/apply$/.test(rest)) return "apply";
  }
  if (env === null) return "";
  if (env === "/alerts")
    return query.get("state") === "resolved" ? "resolved" : "firing";
  if (/^\/alerts\/[^/]+\/acknowledge$/.test(env)) return "acknowledge";
  if (env === "/alert-rules") return method === "GET" ? "rules" : "createRule";
  if (env === "/alert-rules/preview") return "preview";
  if (/^\/alert-rules\/[^/]+$/.test(env))
    return method === "PUT" ? "updateRule" : "deleteRule";
  if (env === "/alert-presets") return "presets";
  if (env === "/alert-channels")
    return method === "GET" ? "channels" : "createChannel";
  if (/^\/alert-channels\/[^/]+\/test$/.test(env)) return "testChannel";
  if (/^\/alert-channels\/[^/]+$/.test(env))
    return method === "PUT" ? "updateChannel" : "deleteChannel";
  if (env === "/alert-settings")
    return method === "GET" ? "settings" : "saveSettings";
  if (env === "/alert-recipient-consents")
    return method === "GET" ? "consents" : "recordConsent";
  if (/^\/alert-recipient-consents\/[^/]+\/revoke$/.test(env)) return "revoke";
  if (env === "/subscriptions") return "subscriptions";
  if (env === "/operations/summary") return "summary";
  if (env === "/operations/retention-periods") return "periods";
  if (env === "/bindable-connections") return "connections";
  if (env === "/deployment-targets") return "targets";
  return "";
}

/** What each operation answers unless a test says otherwise. */
const defaults = (): Record<string, Answer> => ({
  firing: fixture.alertPage(fixture.firing()),
  resolved: fixture.alertPage(fixture.resolved()),
  rules: { items: fixture.rules(), next_cursor: null },
  presets: fixture.presets(),
  channels: { items: fixture.channels(), next_cursor: null },
  settings: fixture.settings(),
  consents: { items: fixture.consents(), next_cursor: null },
  subscriptions: fixture.subscriptions(),
  summary: {
    observed_at: fixture.NOW,
    api: {
      version: "0.1.0a17",
      build: `sha256:26ab04e1${"0".repeat(56)}`,
      instance: "9b000000-0000-4000-8000-000000000001",
      uptime_seconds: 274_000,
      compatibility: { mode: "ready", findings: 0 },
    },
    health: [{ component: "alert_evaluator", state: "ok" }],
  },
  periods: fixture.retentionPeriods(),
  connections: fixture.mailConnections(),
  targets: fixture.targets(),
  members: fixture.members(),
  address: fixture.myAddress(),
  preview: fixture.previewNoData(),
  acknowledge: (request: Request) => ({
    ...fixture.firing().find((alert) => request.url().includes(alert.id))!,
    acknowledged_at: fixture.NOW,
    acknowledged_by: fixture.ME,
  }),
  createRule: (request: Request) => ({
    ...fixture.rules()[1],
    ...(request.postDataJSON() as object),
    id: "d1000000-0000-4000-8000-000000000010",
    preset_id: undefined,
    state: {
      state: "ok",
      since: fixture.NOW,
      no_data: true,
      unit: "ratio",
    },
  }),
  updateRule: (request: Request) => {
    const id = new URL(request.url()).pathname.split("/").pop();
    const rule = fixture.rules().find((item) => item.id === id)!;
    return {
      ...rule,
      ...(request.postDataJSON() as object),
      revision: rule.revision + 1,
    };
  },
  updateChannel: (request: Request) => {
    const id = new URL(request.url()).pathname.split("/").pop();
    const channel = fixture.channels().find((item) => item.id === id)!;
    const draft = request.postDataJSON() as { recipients: unknown[] };
    return {
      ...channel,
      ...draft,
      owner_id: fixture.ME,
      revision: channel.revision + 1,
      recipient_states: draft.recipients.map((_, index) => ({
        index,
        state: "ok",
      })),
    };
  },
  createChannel: (request: Request) => {
    const draft = request.postDataJSON() as { recipients: unknown[] };
    return {
      ...draft,
      id: "c1000000-0000-4000-8000-000000000003",
      owner_id: fixture.ME,
      revision: 1,
      recipient_states: draft.recipients.map((_, index) => ({
        index,
        state: "ok",
      })),
      status: "ok",
    };
  },
  testChannel: {
    channel_id: fixture.ids.team,
    kind: "test",
    state: "pending",
    at: fixture.NOW,
  },
  compatibility: {
    policy: "weave/operations-v1",
    mode: "ready",
    complete: true,
    inspected: 1204,
    checked_at: "2026-11-18T08:02:00Z",
    findings: [],
    findings_truncated: false,
  },
  check: {
    policy: "weave/operations-v1",
    mode: "restricted",
    complete: true,
    inspected: 1210,
    checked_at: fixture.NOW,
    findings: [{ kind: "worker", code: "worker_protocol_unsupported" }],
    findings_truncated: false,
  },
  telemetry: {
    enabled: false,
    traces: { configured: false, host: null },
    metrics: { configured: false, host: null, interval_seconds: null },
    dropped: 0,
  },
  plan: {
    id: "7a000000-0000-4000-8000-000000000001",
    scope: { tenant_id: "tenant" },
    principal_id: fixture.ME,
    created_at: fixture.NOW,
    cutoff: "2026-11-11T10:54:10Z",
    expires_at: "2026-11-18T14:32:00Z",
    complete: true,
    candidates: [
      ...Array.from({ length: 12 }, (_, index) => ({
        id: `7b000000-0000-4000-8000-0000000000${String(index).padStart(2, "0")}`,
        revision: 1,
        reason: "expired_debug",
      })),
      ...Array.from({ length: 2 }, (_, index) => ({
        id: `7c000000-0000-4000-8000-0000000000${String(index).padStart(2, "0")}`,
        revision: 1,
        reason: "referenced",
      })),
    ],
  },
  apply: {
    plan_id: "7a000000-0000-4000-8000-000000000001",
    deleted: Array.from({ length: 12 }, (_, index) => `7b${index}`),
    blocked: [],
  },
  saveSettings: (request: Request) => ({
    ...(request.postDataJSON() as object),
    revision: fixture.settings().revision + 1,
  }),
  recordConsent: (request: Request) => {
    const draft = request.postDataJSON() as { address: string };
    return {
      ...fixture.consents()[0],
      ...draft,
      id: "e2000000-0000-4000-8000-000000000002",
      domain: draft.address.split("@")[1],
      created_by: fixture.ME,
      created_at: fixture.NOW,
    };
  },
  revoke: {
    ...fixture.consents()[0],
    revoked_at: fixture.NOW,
    revoked_by: fixture.ME,
  },
  saveAddress: (request: Request) => ({
    ...(request.postDataJSON() as object),
    updated_at: fixture.NOW,
  }),
});

/**
 * Connects as the fixture's person with `capabilities` in local at 10:54,
 * answers the alert operations from the fixture, and opens `path`.
 */
export async function alertsPlatform(
  page: Page,
  options: {
    capabilities?: string[];
    answers?: Record<string, Answer>;
    path?: string;
  } = {},
): Promise<AlertsPlatform> {
  const capabilities = options.capabilities ?? manager;
  await page.clock.install({ time: new Date(fixture.NOW) });
  await connected(page, { capabilities, environmentName: "local" });
  const platform: AlertsPlatform = {
    sent: [],
    answers: { ...defaults(), ...options.answers },
  };
  await page.route("**/studio/api/api/v1/identity", (route) =>
    route.fulfill({
      json: {
        principal_id: fixture.ME,
        kind: "human",
        grants: [
          {
            role: "test",
            scope: {
              tenant_id: "tenant",
              project_id: "project",
              environment_id: "development",
            },
            resources: [],
            capabilities,
          },
        ],
        workspaces: [
          {
            id: "tenant",
            name: "Acme",
            projects: [
              {
                id: "project",
                name: "Billing",
                environments: [{ id: "development", name: "local" }],
              },
            ],
          },
        ],
        truncated: false,
      },
    }),
  );
  await page.route(
    (url) => url.pathname.startsWith(TENANT),
    async (route: Route) => {
      const request = route.request();
      const url = new URL(request.url());
      const name = operation(request.method(), url.pathname, url.searchParams);
      if (!name) return route.fallback();
      let body: unknown = null;
      try {
        body = request.postDataJSON();
      } catch {
        body = request.postData();
      }
      platform.sent.push({
        method: request.method(),
        path: url.pathname,
        query: url.searchParams,
        body,
      });
      const answer = platform.answers[name];
      if (answer === undefined) return route.fallback();
      if (typeof answer === "number")
        return route.fulfill({
          status: answer,
          json: problems[answer] ?? { code: "WV-ERROR", message: "Failed" },
        });
      if (typeof answer === "function")
        return route.fulfill({ json: answer(request) });
      if (
        answer &&
        "status" in answer &&
        "json" in answer &&
        typeof (answer as { status: unknown }).status === "number"
      )
        return route.fulfill(answer as { status: number; json: unknown });
      return route.fulfill({ json: answer });
    },
  );
  await page.goto(options.path ?? "/operate/alerts");
  return platform;
}

/** The requests of one operation, in order. */
export const sentTo = (
  platform: AlertsPlatform,
  path: string,
  method = "GET",
) =>
  platform.sent.filter(
    (request) => request.path.endsWith(path) && request.method === method,
  );
