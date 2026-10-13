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
// A published workflow and the environment its activation reads: connection
// revisions, connector and worker releases, and human task assignments, so
// the activation dialog can be opened with its pins.
import { expect, type Page } from "@playwright/test";
import {
  command,
  connected,
  newWorkflow,
  type CatalogEntry,
  workerAction,
} from "./support";
import { DesignerPage } from "./designer-po";

const digest =
  "eddfa829184f8505fd0e1bc7a84b490fc57b555a39495b9724b2728b277133d8";
export const connectorVersion = "7a8cbeef-7d5c-483a-b926-4157ad4286d0";
export const httpRelease = "3f2a9b1c-0000-4000-8000-000000000001";
const otherRelease = "3f2a9b1c-0000-4000-8000-000000000009";
export const workerRelease = "3f2a9b1c-0000-4000-8000-000000000002";
export const binding = "3f2a9b1c-0000-4000-8000-000000000003";
export const revision = "0f8fad5b-d9cb-469f-a165-70867728950e";
export const versionId = "11111111-1111-4111-8111-111111111111";

const getPet = {
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "get-pet", version: "1.0.0" },
  spec: {
    implementation: {
      kind: "connector",
      uses: "weave-http@2.0.0",
      action: "read",
    },
    connection: { connector: "weave-http@2.0.0" },
    sideEffect: "read_only",
    timeoutSeconds: 30,
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
  },
};
const catalog = [
  { id: "h1", document: getPet },
  { id: "a2", document: workerAction },
] as unknown as CatalogEntry[];
export const source = `apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: pets
  version: 1.0.0
spec:
  inputSchema:
    type: object
  outputSchema:
    type: object
  connections:
    pets:
      connector: weave-http@2.0.0
  steps:
    - id: fetch
      kind: action
      uses: get-pet@1.0.0
      connection: pets
      with:
        literal: {}
    - id: crm
      kind: action
      uses: crm.lookup@2.0.0
      with:
        literal:
          customer: c-1
    - id: review
      kind: humanTask
      assignment: reviewers
      title:
        literal: Review
      context:
        literal: {}
      formSchema:
        type: object
      decisions: [approve, reject]
  output:
    literal: {}
`;
const binding404 = { code: "WV-NOT-FOUND", message: "Not found" };

export interface Platform {
  releases?: number;
  assignments?: number;
  /** Fail the version export, so the pins come from the editor. */
  exportStatus?: number;
}
/** The environment, project and publish mocks; returns the captured activation body. */
export async function platform(page: Page, options: Platform = {}) {
  const captured: {
    body: Record<string, unknown> | null;
    exports: number;
    contracts: string[];
  } = { body: null, exports: 0, contracts: [] };
  captured.contracts = (await connected(page, { catalog })).exports;
  await page.route("**/environments/development/connections?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: revision,
            name: "pets",
            revision: 1,
            connector: "weave-http@2.0.0",
            adapter: "weave-http-v2",
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/projects/project/connectors?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: connectorVersion,
            kind: "Connector",
            name: "weave-http",
            version: "2.0.0",
            digest: "a".repeat(64),
            definition_digest: digest,
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/projects/project/connector-descriptors/*", (r) =>
    r.fulfill({ status: 404, json: binding404 }),
  );
  await page.route("**/environments/development/worker-releases?*", (r) =>
    options.releases
      ? r.fulfill({ status: options.releases, json: { code: "WV-DENIED" } })
      : r.fulfill({
          json: {
            items: [
              {
                id: httpRelease,
                image_digest: "sha256:" + "b".repeat(64),
                capabilities: [
                  {
                    taskType: "weave-connector-http-read",
                    taskVersion: "2.0.0",
                  },
                  {
                    taskType: "weave-connector-http-write",
                    taskVersion: "2.0.0",
                  },
                ],
                connector_bindings: [
                  {
                    connector_digest: digest,
                    action: "read",
                    adapter: "weave-http-v2",
                    implementation_version: "2.0.0",
                    task_reference: "weave-connector-http-read@2.0.0",
                  },
                  {
                    connector_digest: digest,
                    action: "write",
                    adapter: "weave-http-v2",
                    implementation_version: "2.0.0",
                    task_reference: "weave-connector-http-write@2.0.0",
                  },
                ],
              },
              {
                // Bound to another connector digest: never offered.
                id: otherRelease,
                image_digest: "sha256:" + "c".repeat(64),
                capabilities: [
                  {
                    taskType: "weave-connector-http-read",
                    taskVersion: "2.0.0",
                  },
                ],
                connector_bindings: [
                  {
                    connector_digest: "1".repeat(64),
                    action: "read",
                    adapter: "weave-http-v2",
                    task_reference: "weave-connector-http-read@2.0.0",
                  },
                ],
              },
              {
                id: workerRelease,
                image_digest: "sha256:" + "d".repeat(64),
                capabilities: [
                  { taskType: "crm-lookup", taskVersion: "1.0.0" },
                ],
              },
            ],
            next_cursor: null,
          },
        }),
  );
  await page.route("**/environments/development/human-assignments", (r) =>
    options.assignments
      ? r.fulfill({ status: options.assignments, json: { code: "WV-DENIED" } })
      : r.fulfill({
          json: {
            items: [
              {
                binding_id: binding,
                name: "reviewers",
                enabled: true,
                revision: 2,
                principal_ids: ["8c1f1c55-0000-4000-8000-000000000001"],
                group_ids: [],
              },
            ],
          },
        }),
  );
  await page.route("**/projects/project/workflows", (r) =>
    r.fulfill({
      status: 201,
      json: {
        id: versionId,
        name: "pets",
        version: "1.0.0",
        digest: "e".repeat(64),
      },
    }),
  );
  await page.route(`**/projects/project/workflows/${versionId}/export`, (r) => {
    captured.exports++;
    if (options.exportStatus)
      return r.fulfill({ status: options.exportStatus, json: binding404 });
    return r.fulfill({
      json: {
        id: versionId,
        document: {
          spec: { connections: { pets: { connector: "weave-http@2.0.0" } } },
        },
        artifact: {
          executable: {
            connections: {
              pets: { connector: "weave-http@2.0.0", required: true },
            },
            dependencies: [
              {
                kind: "Action",
                reference: "get-pet@1.0.0",
                digest: "2".repeat(64),
                document: getPet,
              },
              {
                kind: "Action",
                reference: "crm.lookup@2.0.0",
                digest: "3".repeat(64),
                document: workerAction,
              },
              {
                kind: "Connector",
                reference: "weave-http@2.0.0",
                digest,
                document: { spec: { adapter: "weave-http-v2" } },
              },
            ],
            graph: {
              nodes: [
                { kind: "humanTask", id: "review", assignment: "reviewers" },
              ],
            },
          },
        },
      },
    });
  });
  await page.route("**/environments/development/activations", (r) => {
    captured.body = r.request().postDataJSON();
    return r.fulfill({
      status: 201,
      json: { id: "activation", name: "pets", revision: 1 },
    });
  });
  return captured;
}

/** Opens the published workflow's activation dialog with the crm contract loaded. */
export async function openActivation(
  page: Page,
  contracts: string[],
  review = true,
) {
  const designer = new DesignerPage(page);
  await newWorkflow(page);
  await designer.setSource(source);
  // Selecting the worker action loads its contract, so its task type is known.
  await designer.selectStep("crm");
  await expect.poll(() => contracts).toContain("a2");
  // On narrow layouts the inspector covers the toolbar; close it first.
  const close = page.getByRole("button", { name: "Close inspector" });
  if (await close.isVisible()) await close.click();
  await command(page, "Publish…");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish version" })
    .click();
  await command(page, "Activate…");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(/Activate \S+ \d+\.\d+\.\d+/);
  await expect(dialog.getByRole("status")).toHaveCount(0);
  const reviewButton = dialog.getByRole("button", {
    name: "Review",
    exact: true,
  });
  if (review && (await reviewButton.isVisible())) await reviewButton.click();
  return dialog;
}
