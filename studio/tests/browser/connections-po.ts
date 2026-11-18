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
// A platform with connections for step details: per environment, the
// connections this person may bind, what a connection test answers and the
// test policy, on the fake Studio host of platform-host.ts. Every request is
// recorded so a spec can say exactly what Studio asked.
import { expect, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { useNewEditor } from "./canvas-po";
import { acme, acmePlatform, platformHost } from "./platform-host";
import { StepDetailsPage, yamlFile } from "./step-details-po";
import { allCapabilities } from "./support";

export const PRODUCTION = acme[0].environment_id;
export const STAGING = acme[1].environment_id;
export const HTTP = "weave-http@2.0.0";

const id = (n: number) =>
  `00000000-0000-4000-9000-${String(n).padStart(12, "0")}`;
/** A connection this person may bind, as `bindable_connections.list` lists it. */
export function bindable(
  n: number,
  over: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    connection_id: id(n),
    revision_id: id(1000 + n),
    name: `acme-crm-${n}`,
    connector: HTTP,
    label: `acme-crm-${n}`,
    enabled: true,
    endpoint_origin: `https://api${n}.acme.test`,
    ...over,
  };
}
export const connectionId = (n: number) => id(n);
export const revisionId = (n: number) => id(1000 + n);

/** The published action the step calls: one HTTP request through a connection. */
export const crmLookup = {
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "acme.crm-lookup", version: "1.0.0" },
  spec: {
    implementation: { kind: "connector", uses: HTTP, action: "request" },
    connection: { connector: HTTP },
    sideEffect: "read_only",
    timeoutSeconds: 30,
    inputSchema: {
      type: "object",
      required: ["customerId"],
      properties: { customerId: { type: "string", title: "Customer ID" } },
    },
    outputSchema: {
      type: "object",
      properties: { name: { type: "string" }, open: { type: "number" } },
    },
  },
};

/** A workflow whose step calls the action, with these slots declared. */
export function crmWorkflow(
  slots: Record<string, string> = {},
  step: Record<string, unknown> = {},
) {
  return yamlFile(
    "order-intake.yaml",
    stringify({
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "order-intake", version: "1.0.0" },
      spec: {
        inputSchema: {
          type: "object",
          properties: { customerId: { type: "string" } },
        },
        outputSchema: { type: "object" },
        ...(Object.keys(slots).length
          ? {
              connections: Object.fromEntries(
                Object.entries(slots).map(([name, connector]) => [
                  name,
                  { connector, required: true },
                ]),
              ),
            }
          : {}),
        steps: [
          {
            id: "crm-lookup",
            kind: "action",
            uses: "acme.crm-lookup@1.0.0",
            with: { object: { customerId: { ref: "/input/customerId" } } },
            ...step,
          },
        ],
      },
    }),
  );
}

export interface ConnectionsState {
  /** Bindable connections per environment ID. */
  bindable: Record<string, Record<string, unknown>[]>;
  /** What a connection test answers, by revision ID; ok when absent. */
  tests?: Record<
    string,
    Record<string, unknown> | { status: number; json: unknown }
  >;
  /** The test policy per environment ID; "enabled" when absent, 403 for a person who can't read it. */
  policy?: Record<string, "enabled" | "disabled" | 403>;
  /** Milliseconds to hold the first bindable answer of an environment. */
  holdFirst?: Record<string, Promise<void>>;
  capabilities?: string[];
}
export interface ConnectionsRecorder {
  bindable: string[];
  tests: string[];
  policy: string[];
}

const environmentOf = (url: string) =>
  /\/environments\/([^/?]+)/.exec(url)?.[1] ?? "";

/** Answers the platform's connection operations from `state`, recording each request. */
export async function routeConnections(
  page: Page,
  state: ConnectionsState,
): Promise<ConnectionsRecorder> {
  const recorder: ConnectionsRecorder = { bindable: [], tests: [], policy: [] };
  const held = new Set<string>();
  await page.route(
    "**/environments/*/bindable-connections?*",
    async (route) => {
      const url = route.request().url();
      const environment = environmentOf(url);
      recorder.bindable.push(url);
      const hold = state.holdFirst?.[environment];
      if (hold && !held.has(environment)) {
        held.add(environment);
        await hold;
      }
      await route
        .fulfill({
          json: { items: state.bindable[environment] ?? [], next_cursor: null },
        })
        .catch(() => undefined);
    },
  );
  await page.route("**/environments/*/connections/*/test", async (route) => {
    const revision = /\/connections\/([^/]+)\/test/.exec(
      route.request().url(),
    )![1];
    recorder.tests.push(revision);
    const answer = state.tests?.[revision];
    if (answer && "status" in answer && typeof answer["status"] === "number")
      return route.fulfill({ status: answer["status"], json: answer["json"] });
    await route.fulfill({
      json: answer ?? { ok: true, code: "ok", encrypted: true },
    });
  });
  await page.route("**/environments/*/test-policy", async (route) => {
    const environment = environmentOf(route.request().url());
    recorder.policy.push(environment);
    const policy = state.policy?.[environment] ?? "enabled";
    if (policy === 403)
      return route.fulfill({
        status: 403,
        json: { code: "WV-FORBIDDEN", message: "Forbidden" },
      });
    await route.fulfill({ json: { test_calls: policy, revision: 1 } });
  });
  return recorder;
}

/**
 * Studio connected to Acme / Payments / Production on the fake host, with the
 * new editor, the action in the project's catalog and the connection
 * operations answered from `state`. Opens `file` when given.
 */
export async function openConnected(
  page: Page,
  state: ConnectionsState,
  file?: ReturnType<typeof yamlFile>,
): Promise<{ details: StepDetailsPage; recorder: ConnectionsRecorder }> {
  await useNewEditor(page);
  const capabilities = state.capabilities ?? [...allCapabilities, "compile"];
  await platformHost(page, {
    platforms: [acmePlatform()],
    active: "Acme",
    test: (host) => ({
      json: {
        session: host.session(),
        identity: {
          ...host.identity(),
          grants: host
            .identity()
            .grants.map((grant) => ({ ...grant, capabilities })),
        },
        workspaces: host.current?.workspaces ?? [],
        truncated: false,
        workspace_revoked: false,
      },
    }),
  });
  await page.route("**/projects/*/actions?*", (route) =>
    route.fulfill({
      json: {
        items: [{ id: "h1", name: "acme.crm-lookup", version: "1.0.0" }],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/projects/*/actions/h1/export", (route) =>
    route.fulfill({
      json: {
        id: "h1",
        name: "acme.crm-lookup",
        version: "1.0.0",
        document: crmLookup,
      },
    }),
  );
  await page.route(
    /\/studio\/(?:contracts\/language|api\/api\/v1\/tenants\/[^/]+\/projects\/[^/]+\/language)$/,
    (route) => route.fulfill({ json: { features: ["text.concat"] } }),
  );
  const recorder = await routeConnections(page, state);
  const details = new StepDetailsPage(page);
  if (file) {
    await page.getByLabel("Choose a workflow file").setInputFiles(file);
    await expect(page.locator(".editor-bar")).toBeVisible();
    const show = page.getByRole("button", { name: "Show canvas", exact: true });
    if (await show.isVisible()) await show.click();
  }
  return { details, recorder };
}

/** The Connection field's select in the open step details. */
export const connectionSelect = (details: StepDetailsPage) =>
  details.field("connection").getByRole("combobox", { name: "Connection" });

/** What this computer keeps for the account: every key and value Studio wrote to browser storage. */
export async function keptOnThisComputer(page: Page): Promise<string> {
  return page.evaluate(() =>
    JSON.stringify({
      local: Object.fromEntries(
        Array.from({ length: localStorage.length }, (_, i) => {
          const key = localStorage.key(i)!;
          return [key, localStorage.getItem(key)];
        }),
      ),
      session: Object.fromEntries(
        Array.from({ length: sessionStorage.length }, (_, i) => {
          const key = sessionStorage.key(i)!;
          return [key, sessionStorage.getItem(key)];
        }),
      ),
    }),
  );
}
