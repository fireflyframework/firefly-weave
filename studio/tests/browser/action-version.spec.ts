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
// Change in a new version: a published action's retry, timeout or request
// change in a draft of its next version on this computer, which the
// workflow waits for before it publishes.
import { expect, test, type Page } from "@playwright/test";
import { parse } from "yaml";
import {
  allCapabilities,
  command,
  connected,
  defaultCatalog,
  sourceText,
  type CatalogEntry,
} from "./support";
import {
  openStepFixture,
  sizes,
  stepFixture,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";

const TENANT = "00000000-0000-4000-8000-000000000031";
const PROJECT = "00000000-0000-4000-8000-000000000032";

const httpAction = {
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "onboarding.check-customer", version: "1.0.0" },
  spec: {
    implementation: {
      kind: "http",
      method: "GET",
      url: "https://api.example.com/customers",
    },
    sideEffect: "read_only",
    timeoutSeconds: 20,
    retry: { maxAttempts: 3, initialDelaySeconds: 1, maxDelaySeconds: 30 },
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
  },
};
const workflow = yamlFile(
  "onboarding.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: { name: onboarding, version: 1.0.0 }
spec:
  inputSchema: { type: object }
  outputSchema: { type: object }
  steps:
    - id: check
      kind: action
      uses: onboarding.check-customer@1.0.0
      with: { literal: {} }
    - id: crm
      kind: action
      uses: crm.lookup@2.0.0
      with: { literal: { customer: "c-1" } }
  output: { literal: {} }
`,
);
const catalog: CatalogEntry[] = [
  ...defaultCatalog,
  { id: "a9", document: httpAction as unknown as CatalogEntry["document"] },
];

async function open(page: Page, file = workflow) {
  const posts: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/actions"))
      posts.push(request.url());
  });
  const details = await openStepFixture(page, file, async (p) => {
    const result = await connected(p, {
      capabilities: [...allCapabilities, "compile"],
      catalog,
      tenantId: TENANT,
      projectId: PROJECT,
    });
    await withLanguageFeatures(p);
    return result;
  });
  return { details, posts };
}
const kept = (page: Page) =>
  page.evaluate(() =>
    Object.entries(localStorage)
      .filter(([key]) => key.includes("weave.actionDraft."))
      .map(([, value]) => JSON.parse(value)),
  );

for (const size of sizes)
  test.describe(`Change in a new version at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("changing a published action's retry starts its next version", async ({
      page,
    }) => {
      const { details, posts } = await open(page, stepFixture);
      await details.open("lookup");
      await details.tab("Settings").click();
      const retry = details.field("retry");
      await expect(retry).toContainText("Set by sql.lookup@1.0.0.");
      await retry
        .getByRole("button", { name: "Change in a new version" })
        .click();
      await expect(retry).toContainText(
        "Saved in sql.lookup 1.0.1 on this computer. Publish it before publishing this workflow.",
      );
      const tries = details
        .field("retry.maxAttempts")
        .getByRole("textbox", { name: "Max tries" });
      await expect(tries).toBeEditable();
      await tries.fill("5");
      await tries.press("Tab");
      await expect
        .poll(async () =>
          (await kept(page)).map(
            (entry) => entry.document.spec.retry.maxAttempts,
          ),
        )
        .toEqual([5]);
      await details.close();
      expect(parse(await sourceText(page)).spec.steps[1].uses).toBe(
        "sql.lookup@1.0.1",
      );
      expect(posts).toEqual([]);
    });

    test("a worker action says where its retry is set", async ({ page }) => {
      const { details } = await open(page);
      await details.open("crm");
      await details.tab("Settings").click();
      await expect(details.field("timeout")).toContainText(
        "Set by the worker that runs this action.",
      );
      await expect(
        details.dialog.getByRole("button", { name: "Change in a new version" }),
      ).toHaveCount(0);
    });

    test("an HTTP action's request changes as YAML", async ({ page }) => {
      const { details } = await open(page);
      await details.open("check");
      await details.tab("Settings").click();
      await details
        .field("retry")
        .getByRole("button", { name: "Change in a new version" })
        .click();
      await details.dialog
        .getByRole("button", { name: "More actions for check" })
        .click();
      await page.getByRole("menuitem", { name: "Edit action as YAML" }).click();
      const dialog = page.getByRole("dialog", {
        name: "Edit onboarding.check-customer 1.0.1 as YAML",
      });
      const text = dialog.getByRole("textbox", { name: "Action YAML" });
      const yaml = (await text.inputValue()).replace(
        "https://api.example.com/customers",
        "https://api.example.com/v2/customers",
      );
      await text.fill(yaml);
      await dialog.getByRole("button", { name: "Apply" }).click();
      await expect(dialog).toHaveCount(0);
      await expect
        .poll(async () =>
          (await kept(page)).map(
            (entry) => entry.document.spec.implementation.url,
          ),
        )
        .toEqual(["https://api.example.com/v2/customers"]);
    });

    test("publishing the workflow waits for the action", async ({ page }) => {
      const { details } = await open(page);
      await page.route(`**/projects/${PROJECT}/compiler/compile`, (route) =>
        route.fulfill({
          json: {
            validationOk: false,
            errorCount: 1,
            diagnostics: [
              {
                code: "WV-COMP-UNKNOWN_ACTION",
                severity: "error",
                stage: "resolution",
                path: "/spec/steps/0/uses",
                message: "Unknown action",
              },
            ],
          },
        }),
      );
      await details.open("check");
      await details.tab("Settings").click();
      await details
        .field("retry")
        .getByRole("button", { name: "Change in a new version" })
        .click();
      await expect(details.field("retry")).toContainText("Publish action");
      await details.close();
      await command(page, "Publish…");
      await expect(
        page
          .getByText(
            "Publish onboarding.check-customer@1.0.1 before publishing this workflow.",
          )
          .first(),
      ).toBeVisible();
    });
  });
