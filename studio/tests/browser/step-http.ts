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
// A workflow kept on this computer whose Call an API step uses an action the
// workflow owns, opened the way a person reopens it: from Home's recent
// workflows. The repository's own Python builds the action.
import { expect, type Page } from "@playwright/test";
import { localAuthoring } from "./local-authoring";
import { StepDetailsPage, withNewEditor } from "./step-details-po";
import { offline } from "./support";

export const httpWorkflow = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: order-intake, version: 1.3.0}
spec:
  inputSchema:
    type: object
    properties:
      customerId: {type: string}
      status: {type: string}
  outputSchema: {type: object}
  connections:
    crm: {connector: weave-http@2.0.0, required: true}
  steps:
    - id: get-customer-orders
      kind: action
      uses: order-intake.get-customer-orders@1.0.0
      connection: crm
      with:
        object:
          path:
            object:
              customerId: {ref: /input/customerId}
          query:
            object:
              limit: {literal: "25"}
              status: {ref: /input/status}
  output: {literal: {}}
`;

/** What else the stored canvas holds: more of the action's recipe (its retry), step notes. */
export interface HttpCanvasExtras {
  recipe?: Record<string, unknown>;
  stepNotes?: Record<string, { text: string; showOnCanvas?: boolean }>;
}

/** Opens get-customer-orders, whose owned action calls `pathTemplate` with GET. */
export async function openHttpStep(
  page: Page,
  pathTemplate = "/customers/{customerId}/orders",
  source = httpWorkflow,
  extras: HttpCanvasExtras = {},
): Promise<StepDetailsPage> {
  await withNewEditor(page);
  await page.addInitScript(
    ({ source, pathTemplate, extras }) => {
      const id = "2f6c1a40-0000-4000-8000-000000000001";
      const savedAt = "2026-10-11T10:04:00.000Z";
      // Only the first load stores the draft: a reload keeps Studio's own saves.
      if (localStorage.getItem(`weave.localDraft.${id}`)) return;
      localStorage.setItem(
        `weave.localDraft.${id}`,
        JSON.stringify({
          source,
          format: "yaml",
          layout: null,
          canvas: {
            schemaVersion: 2,
            kind: "weave.studio/canvas",
            ownedActions: ["order-intake.get-customer-orders"],
            actionRecipes: {
              "order-intake.get-customer-orders": {
                kind: "http",
                method: "GET",
                pathTemplate,
                statuses: [200],
                timeoutSeconds: 30,
                ...extras.recipe,
              },
            },
            ...(extras.stepNotes ? { stepNotes: extras.stepNotes } : {}),
          },
          savedAt,
        }),
      );
      localStorage.setItem(
        "weave.localDrafts.v1",
        JSON.stringify([
          { id, name: "order-intake", version: "1.3.0", savedAt },
        ]),
      );
    },
    { source, pathTemplate, extras },
  );
  await localAuthoring(page);
  await offline(page);
  await page
    .locator(".preview-row", { hasText: "order-intake" })
    .first()
    .click();
  await expect(page.locator(".editor-bar")).toBeVisible();
  const show = page.getByRole("button", { name: "Show canvas", exact: true });
  const canvas = page.getByLabel("Workflow canvas", { exact: true });
  await expect(canvas.or(show).first()).toBeVisible();
  if (await show.isVisible()) await show.click();
  const details = new StepDetailsPage(page);
  await details.open("get-customer-orders");
  return details;
}
