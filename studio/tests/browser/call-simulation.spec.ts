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
// Executing a workflow that calls another: a draft on this computer goes with
// its source and runs in the test; a call whose workflow the host doesn't
// have stops with the call's words, and test data for the call fixes it; a
// pin on the call wins over the workflow. The repository's own host code
// answers (localAuthoring).
import { expect, test, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { executeButton, executions } from "./execute-po";
import { localAuthoring } from "./local-authoring";
import {
  openStepFixture,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
import { connected, defaultCatalog } from "./support";
import { lookupContract, outputPane, PROJECT, TENANT } from "./test-data-po";

const lookupCustomer = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "lookup-customer", version: "0.1.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: {
      type: "object",
      properties: { name: { type: "string" } },
      required: ["name"],
    },
    callable: {},
    steps: [
      { id: "find", kind: "transform", value: { literal: { name: "Ada" } } },
    ],
    output: { ref: "/steps/find/output" },
  },
};
const orderIntake = (uses: string) =>
  yamlFile(
    "order-intake.yaml",
    stringify({
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "order-intake", version: "1.2.0" },
      spec: {
        inputSchema: { type: "object" },
        outputSchema: { type: "object" },
        steps: [
          {
            id: "lookup",
            kind: "callWorkflow",
            uses,
            mode: "wait",
            with: { literal: {} },
          },
        ],
        output: { ref: "/steps/lookup/output" },
      },
    }),
  );

/** order-intake calling `uses`, with lookup-customer 0.1.0 kept on this computer (seeded before the page loads). */
async function open(page: Page, uses: string) {
  const details = await openStepFixture(page, orderIntake(uses), async (p) => {
    await p.addInitScript((kept) => {
      if (localStorage.getItem("local:weave.localDrafts.v1")) return;
      localStorage.setItem(
        "local:weave.localDrafts.v1",
        JSON.stringify([
          {
            id: "d-lookup",
            name: "lookup-customer",
            version: "0.1.0",
            savedAt: "2026-11-18T10:00:00Z",
          },
        ]),
      );
      localStorage.setItem(
        "local:weave.localDraft.d-lookup",
        JSON.stringify({
          source: kept,
          format: "yaml",
          layout: null,
          savedAt: "2026-11-18T10:00:00Z",
        }),
      );
    }, stringify(lookupCustomer));
    const result = await connected(p, {
      catalog: [...defaultCatalog, { id: "a3", document: lookupContract }],
      tenantId: TENANT,
      projectId: PROJECT,
    });
    await withLanguageFeatures(p, ["flow.callWorkflow"]);
    // The host's test data check for a pin on a call, as the repository's
    // check_test_data answers it: fine when a Workflow definition gives the
    // called workflow's output, unresolved otherwise. Other entries go on to
    // the shared mock.
    await p.route("**/studio/local/testdata/check", async (route) => {
      const body = route.request().postDataJSON() as {
        source: string;
        definitions: Record<string, any>[];
        testData: { pins?: Record<string, unknown> };
      };
      const steps = JSON.parse(body.source).spec.steps as Record<string, any>[];
      const pins = Object.keys(body.testData.pins ?? {});
      const calls = pins.filter(
        (key) =>
          steps.find((step) => step["id"] === key)?.["kind"] === "callWorkflow",
      );
      if (!calls.length) return route.fallback();
      return route.fulfill({
        json: {
          entries: calls.map((key) => {
            const uses = steps.find((step) => step["id"] === key)?.["uses"];
            const known = body.definitions.some(
              (item) =>
                item["kind"] === "Workflow" &&
                `${item["metadata"]?.["name"]}@${item["metadata"]?.["version"]}` ===
                  uses &&
                item["spec"]?.["outputSchema"],
            );
            return known
              ? { path: `/pins/${key}`, ok: true }
              : {
                  path: `/pins/${key}`,
                  ok: false,
                  code: "WV-STUDIO-TESTDATA-UNRESOLVED",
                };
          }),
        },
      });
    });
    return result;
  });
  const calls = await localAuthoring(page);
  await details.open("lookup");
  return { details, calls };
}

test.describe("executing a workflow that calls another", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a draft on this computer goes with its source and runs in the test", async ({
    page,
  }) => {
    const { details, calls } = await open(page, "lookup-customer@0.1.0");
    await executeButton(details.dialog).click();
    await expect.poll(() => executions(calls).length).toBe(1);
    const sent = executions(calls)[0].body["workflows"] as Record<
      string,
      { source?: string; format?: string }
    >;
    expect(sent["lookup-customer@0.1.0"]).toMatchObject({
      source: stringify(lookupCustomer),
      format: "yaml",
    });
    const pane = await outputPane(details);
    await pane.getByRole("radio", { name: "JSON" }).click();
    await expect(pane.locator(".pane-json")).toContainText('"Ada"');
  });

  test("a call whose workflow the host doesn't have stops with the call's words and offers Open workflow", async ({
    page,
  }) => {
    const { details, calls } = await open(page, "notify-customer@1.0.0");
    await executeButton(details.dialog).click();
    await expect.poll(() => executions(calls).length).toBe(1);
    const pane = await outputPane(details);
    const notice = pane.locator(".pane-notice");
    await expect(notice).toContainText(
      "Call notify-customer needs test data or the workflow itself.",
    );
    await expect(
      notice.getByRole("button", { name: "Open workflow" }),
    ).toBeVisible();
    // Studio doesn't know what notify-customer returns, so it can't check test output for the call.
    await expect(
      notice.getByRole("button", { name: "Set test output" }),
    ).toHaveCount(0);
    // Open workflow looks for it: here neither on this computer nor in the project.
    await notice.getByRole("button", { name: "Open workflow" }).click();
    await expect(
      page
        .getByText(
          "Studio can't find notify-customer 1.0.0 on this computer. Connect to a platform to open the project's published workflows.",
        )
        .or(
          page.getByText(
            "Studio can't find notify-customer 1.0.0 in this project.",
          ),
        )
        .first(),
    ).toBeVisible();
  });

  test("a pin on the call wins over the workflow it calls", async ({
    page,
  }) => {
    const { details, calls } = await open(page, "lookup-customer@0.1.0");
    const pane = await outputPane(details);
    await pane.getByRole("button", { name: "Set test output" }).click();
    const editor = pane.locator(".output-editor");
    await editor.getByRole("textbox").first().fill('{ "name": "Pinned" }');
    await pane.getByRole("button", { name: "Save", exact: true }).click();
    await expect(pane.locator(".pinned-banner")).toBeVisible();
    await executeButton(details.dialog).click();
    await expect.poll(() => executions(calls).length).toBe(1);
    expect(executions(calls)[0].body["mocks"]).toEqual({
      "node:lookup": { name: "Pinned" },
    });
    await pane.getByRole("radio", { name: "JSON" }).click();
    await expect(pane.locator(".pane-json")).toContainText('"Pinned"');
  });
});
