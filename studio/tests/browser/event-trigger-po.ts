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
// notify-team, which reacts to platform events: opened connected to a project
// with published workflows (the platform is mocked), with the platform
// event's fields and the local checks answered by the repository's Python.
import type { Locator, Page, Route } from "@playwright/test";
import { expect } from "@playwright/test";
import { parse, stringify } from "yaml";
import { routePlatformEvent, routeValidate } from "./local-authoring";
import {
  openStepFixture,
  StepDetailsPage,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
import { allCapabilities, connected, sourceText } from "./support";

export const REFERENCE = { $ref: "weave.platform-event-v1" };
export const FORM = {
  type: "object",
  required: ["reference"],
  properties: { reference: { type: "string" }, message: { type: "string" } },
};
export const ATTENTION = { events: ["transaction.in_doubt"] };

const published = (name: string, version: string) => ({
  document: {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name, version },
    spec: {
      inputSchema: {},
      outputSchema: {},
      steps: [],
      output: { literal: null },
    },
  },
  digest: "0".repeat(64),
});
const LOCK = {
  definitions: [
    published("move-between-systems", "1.4.0"),
    published("move-files-nightly", "2.3.0"),
    published("monitor-queues", "1.0.4"),
  ],
  tasks: [],
  adapters: [],
  schemas: {},
};

/** notify-team 1.2.0: it reacts to a transaction that needs attention, then sends. */
export const notifyTeam = (spec: Record<string, unknown> = {}) =>
  yamlFile(
    "notify-team.yaml",
    stringify({
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "notify-team", version: "1.2.0" },
      spec: {
        inputSchema: REFERENCE,
        outputSchema: {},
        eventTrigger: ATTENTION,
        steps: [
          {
            id: "send",
            kind: "transform",
            value: { object: { key: { ref: "/input/type" } } },
          },
        ],
        output: { literal: null },
        ...spec,
      },
    }),
  );

/** Workflows kept on this computer, as Studio stores them. */
async function keptDrafts(page: Page, names: string[]) {
  await page.addInitScript((list) => {
    const savedAt = "2026-11-20T09:00:00.000Z";
    localStorage.setItem(
      "local:weave.localDrafts.v1",
      JSON.stringify(
        list.map((name) => ({
          id: `draft-${name}`,
          name,
          version: "1.0.0",
          savedAt,
        })),
      ),
    );
    for (const name of list)
      localStorage.setItem(
        `local:weave.localDraft.draft-${name}`,
        JSON.stringify({ source: "{}", format: "json", savedAt }),
      );
  }, names);
}

export interface NotifyTeamOptions {
  spec?: Record<string, unknown>;
  /** The catalog lock's answer; without catalog.read it is never asked. */
  catalog?: (route: Route) => Promise<void>;
  capabilities?: string[];
  drafts?: string[];
  /** What a check waits for before the host answers it. */
  check?: () => Promise<void>;
  /** The platform event's fields, when the host doesn't answer them as it does. */
  view?: (route: Route) => Promise<void>;
}
export async function openNotifyTeam(
  page: Page,
  options: NotifyTeamOptions = {},
): Promise<StepDetailsPage> {
  const details = await openStepFixture(
    page,
    notifyTeam(options.spec),
    async (p) => {
      await keptDrafts(p, options.drafts ?? []);
      await routePlatformEvent(p);
      if (options.view)
        await p.route("**/studio/contracts/platform-event", options.view);
      const result = await connected(p, {
        capabilities: options.capabilities ?? allCapabilities,
      });
      await routeValidate(p);
      if (options.check)
        await p.route("**/studio/local/validate", async (route) => {
          await options.check!();
          await route.fallback();
        });
      await withLanguageFeatures(p, ["flow.eventTrigger"]);
      await p.route(
        /\/projects\/[^/]+\/catalog$/,
        options.catalog ?? ((route) => route.fulfill({ json: LOCK })),
      );
      return result;
    },
  );
  await expect(eventTile(page)).toBeAttached();
  return details;
}

export const eventTile = (page: Page): Locator =>
  page
    .locator('.tile-node[data-tile^="$trigger"]')
    .filter({ hasText: "Platform event" });
/** Opens the trigger's step details on Parameters. */
export async function openEventTrigger(
  page: Page,
  details: StepDetailsPage,
): Promise<Locator> {
  await eventTile(page).locator(".tile-body").dblclick();
  const dialog = details.dialogFor("Platform event trigger");
  await expect(dialog).toBeVisible();
  return dialog;
}
export const spec = async (page: Page) =>
  (parse(await sourceText(page)) as { spec: Record<string, unknown> }).spec;
/** What the workflow declares, read from its source. */
export const declared = async (page: Page) =>
  (await spec(page))["eventTrigger"];
