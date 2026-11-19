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
// Open workflow: from a call step, the workflow it calls opens, its draft on
// this computer first, otherwise its published version in the project (as
// Studio opens any published version, under an identity of its own). The
// platform is mocked.
import { expect, test, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { connected } from "./support";
import {
  openStepFixture,
  StepDetailsPage,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";

const document = (name: string, version: string, steps: unknown[] = []) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name, version },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    callable: {},
    steps,
    output: { literal: {} },
  },
});
const caller = (uses: string) =>
  stringify({
    ...document("order-intake", "1.2.0", [
      {
        id: "notify",
        kind: "callWorkflow",
        uses,
        mode: "wait",
        with: { literal: {} },
      },
    ]),
  });
const draft = {
  entry: {
    id: "d-lookup",
    name: "lookup-customer",
    version: "0.1.0",
    savedAt: "2026-11-18T10:00:00Z",
  },
  document: {
    source: stringify(document("lookup-customer", "0.1.0")),
    format: "yaml",
    layout: null,
    savedAt: "2026-11-18T10:00:00Z",
  },
};

/** order-intake calling `uses`, connected to a project that publishes notify-customer 1.0.0; lookup-customer is a draft here. */
async function open(page: Page, uses: string) {
  const exports: string[] = [];
  const details = await openStepFixture(
    page,
    yamlFile("order-intake.yaml", caller(uses)),
    async (p) => {
      await p.addInitScript((kept) => {
        if (localStorage.getItem("local:weave.localDrafts.v1")) return;
        localStorage.setItem(
          "local:weave.localDrafts.v1",
          JSON.stringify([kept.entry]),
        );
        localStorage.setItem(
          `local:weave.localDraft.${kept.entry.id}`,
          JSON.stringify(kept.document),
        );
      }, draft);
      const result = await connected(p);
      await withLanguageFeatures(p, ["flow.callWorkflow"]);
      await p.route(/\/projects\/[^/]+\/catalog$/, (route) =>
        route.fulfill({
          json: {
            definitions: [
              {
                document: document("notify-customer", "1.0.0"),
                digest: "0".repeat(64),
              },
            ],
            tasks: [],
            adapters: [],
            schemas: {},
          },
        }),
      );
      await p.route(/\/projects\/[^/]+\/workflows\?/, (route) =>
        route.fulfill({
          json: {
            items: [
              {
                id: "v-notify",
                name: "notify-customer",
                version: "1.0.0",
                digest: "a".repeat(64),
              },
            ],
            next_cursor: null,
          },
        }),
      );
      await p.route(
        /\/projects\/[^/]+\/workflows\/v-notify\/export$/,
        (route) => {
          exports.push(route.request().url());
          return route.fulfill({
            json: {
              id: "v-notify",
              document: document("notify-customer", "1.0.0", [
                { id: "send", kind: "transform", value: { ref: "/input" } },
              ]),
            },
          });
        },
      );
      return result;
    },
  );
  await details.open("notify");
  return { details, exports };
}
const identity = (page: Page) => page.locator(".editor-identity");
const toast = (page: Page, text: string) =>
  page
    .getByRole("status")
    .or(page.getByRole("alert"))
    .filter({ hasText: text })
    .first();
const openWorkflow = (details: StepDetailsPage) =>
  details.field("workflow").getByRole("button", { name: "Open workflow" });

test.describe("opening the workflow a step calls", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a draft on this computer opens as that draft", async ({ page }) => {
    const { details, exports } = await open(page, "lookup-customer@0.1.0");
    await openWorkflow(details).click();
    await expect(identity(page)).toContainText("lookup-customer");
    await expect(details.dialog).toHaveCount(0);
    expect(exports).toEqual([]);
  });

  test("a published version opens under its own identity, and says changes start a new draft", async ({
    page,
  }) => {
    const { details, exports } = await open(page, "notify-customer@1.0.0");
    await openWorkflow(details).click();
    await expect(identity(page)).toContainText("notify-customer");
    await expect(
      toast(
        page,
        "Opened notify-customer 1.0.0 from this project. Your changes start a new draft.",
      ),
    ).toBeVisible();
    expect(exports).toHaveLength(1);
    await expect(page.locator('.tile-node[data-tile="send"]')).toBeVisible();
  });

  test("a version the project doesn't have says so and stays", async ({
    page,
  }) => {
    const { details } = await open(page, "gone-workflow@1.0.0");
    await openWorkflow(details).click();
    // The toast, not the field's hint, which says the same.
    await expect(
      toast(page, "Studio can't find gone-workflow 1.0.0 in this project."),
    ).toBeVisible();
    await expect(identity(page)).toContainText("order-intake");
  });
});
