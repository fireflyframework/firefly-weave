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
// move-between-systems: a workflow kept on this computer whose transaction
// move holds steps, one of them a Call an API step the workflow owns, opened
// from Home as a person reopens it. The language manifest is the
// repository's own; the platform is mocked.
import { expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { parse } from "yaml";
import { repository } from "../python-path";
import {
  hostChecksTestData,
  hostValidate,
  localAuthoring,
  platformCompile,
  type LocalCalls,
} from "./local-authoring";
import {
  openStepFixture,
  StepDetailsPage,
  withNewEditor,
  yamlFile,
} from "./step-details-po";
import {
  allCapabilities,
  connected,
  offline,
  sourceText,
  type CatalogEntry,
} from "./support";

/** The language manifest the Studio host serves, as the repository's Python writes it. */
export const manifest = JSON.parse(
  readFileSync(
    join(repository, "studio/tests/fixtures/language/manifest.json"),
    "utf8",
  ),
).manifest as { features: string[]; step_fields: unknown[] };

export async function withManifest(
  page: Page,
  features: readonly string[] = manifest.features,
) {
  await page.route(
    /\/studio\/(?:contracts\/language|api\/api\/v1\/tenants\/[^/]+\/projects\/[^/]+\/language)$/,
    (route) => route.fulfill({ json: { ...manifest, features } }),
  );
}

/** add, a Call an API step: a request this workflow owns. */
export const ADD_USES = "move-between-systems.add@1.0.0";
export const ADD = `- id: add
  kind: action
  uses: ${ADD_USES}
  connection: target
  with: {object: {body: {object: {quantity: {literal: 1}}}}}
`;
const indented = (text: string, by: number) =>
  text.replace(/^(?=.)/gm, " ".repeat(by));
/** The workflow, with these steps in move, others after it, and more of move. */
export const moveWorkflow = (
  inside: string,
  { after = "", extra = "" }: { after?: string; extra?: string } = {},
) => `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: move-between-systems, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      moveId: {type: string}
      quantity: {type: integer}
    required: [moveId, quantity]
  outputSchema: {}
  connections:
    target: {connector: weave-http@2.0.0, required: true}
  steps:
    - id: move
      kind: transaction
${indented(extra, 6)}      steps:
${indented(inside, 8)}      output: {literal: null}
${indented(after, 4)}  output: {literal: null}
`;

/** Keeps the workflow on this computer, add's request with it, for Home to open. */
export async function keepMove(page: Page, source: string, method = "POST") {
  await page.addInitScript(
    ({ source, method }) => {
      const id = "2f6c1a40-0000-4000-8000-000000000002";
      const savedAt = "2026-10-11T10:04:00.000Z";
      if (localStorage.getItem(`local:weave.localDraft.${id}`)) return;
      localStorage.setItem(
        `local:weave.localDraft.${id}`,
        JSON.stringify({
          source,
          format: "yaml",
          layout: null,
          canvas: {
            schemaVersion: 2,
            kind: "weave.studio/canvas",
            ownedActions: ["move-between-systems.add"],
            actionRecipes: {
              "move-between-systems.add": {
                kind: "http",
                method,
                pathTemplate: "/additions",
                statuses: [200],
                timeoutSeconds: 30,
              },
            },
          },
          savedAt,
        }),
      );
      localStorage.setItem(
        "local:weave.localDrafts.v1",
        JSON.stringify([
          { id, name: "move-between-systems", version: "1.0.0", savedAt },
        ]),
      );
    },
    { source, method },
  );
}
/** Opens the kept workflow from Home, on its canvas. */
export async function openKept(page: Page): Promise<StepDetailsPage> {
  await page
    .locator(".preview-row", { hasText: "move-between-systems" })
    .first()
    .click();
  await expect(page.locator(".editor-bar")).toBeVisible();
  const show = page.getByRole("button", { name: "Show canvas", exact: true });
  const canvas = page.getByLabel("Workflow canvas", { exact: true });
  await expect(canvas.or(show).first()).toBeVisible();
  if (await show.isVisible()) await show.click();
  return new StepDetailsPage(page);
}
interface Definition {
  kind: string;
  metadata: { name: string; version: string };
  spec: Record<string, unknown>;
}
/** The actions, called workflows and worker tasks the transaction tests compile against. */
export const catalog = parse(
  readFileSync(
    join(repository, "tests/fixtures/transactions/catalog.yaml"),
    "utf8",
  ),
) as { definitions: Definition[]; tasks: unknown[] };
const actions = catalog.definitions.filter(({ kind }) => kind === "Action");

/**
 * A platform whose project publishes that catalog. Its own compiler checks a
 * workflow, and so does the Studio host while it is edited.
 */
export async function connectedCatalog(
  page: Page,
  capabilities: string[] = [...allCapabilities, "compile"],
) {
  await connected(page, {
    capabilities,
    catalog: actions.map((document, index) => ({
      id: `a${index}`,
      document: document as unknown as CatalogEntry["document"],
    })),
  });
  await withManifest(page);
  await page.route(/\/projects\/[^/]+\/catalog$/, (route) =>
    route.fulfill({
      json: {
        definitions: catalog.definitions.map((document) => ({
          document,
          digest: "0".repeat(64),
        })),
        tasks: catalog.tasks,
        adapters: [],
        schemas: {},
      },
    }),
  );
  await page.route("**/compiler/compile", async (route) => {
    const body = route.request().postDataJSON() as {
      source: unknown;
      format: string;
    };
    const json = await platformCompile({ ...body, ...catalog });
    await route.fulfill({ json }).catch(() => undefined);
  });
  await hostValidate(page);
}
/** Opens the workflow on that platform, on its canvas. */
export const openConnectedMove = (
  page: Page,
  source: string,
): Promise<StepDetailsPage> =>
  openStepFixture(page, yamlFile("move-between-systems.yaml", source), (p) =>
    connectedCatalog(p),
  );

/** A step of move, as YAML; its other fields one per line. */
export const step = (
  id: string,
  uses: string,
  fields: Record<string, string> = {},
) => `- id: ${id}
  kind: action
  uses: ${uses}
  with: {object: {quantity: {ref: /input/quantity}, reference: {ref: /input/moveId}}}
${Object.entries(fields)
  .map(([name, value]) => `  ${name}: ${value}\n`)
  .join("")}`;

const operation = (uses: string, input: string) =>
  `{kind: action, uses: ${uses}, with: ${input}}`;
/** hold, reserved; add, reversible; submit, which can't be undone; notify after it. */
export const HOLD_STEP = (confirm = "source-system.confirm-hold@1.0.0") =>
  step("hold", "source-system.hold-quantity@1.0.0", {
    confirm: operation(
      confirm,
      confirm.includes("hold-quantity")
        ? "{object: {quantity: {ref: /input/quantity}, reference: {ref: /input/moveId}}}"
        : "{object: {holdId: {ref: /steps/hold/output/holdId}}}",
    ),
    cancel: operation(
      "source-system.release-hold@1.0.0",
      "{object: {reference: {ref: /input/moveId}, holdOperation: {ref: /participant/operationKey}}}",
    ),
  });
export const ADD_STEP = step("add", "target-system.add-quantity@1.0.0", {
  compensate: operation(
    "target-system.remove-quantity@1.0.0",
    "{object: {reference: {ref: /input/moveId}, addOperation: {ref: /participant/operationKey}}}",
  ),
});
export const SUBMIT_STEP = `- id: submit
  kind: action
  uses: orders.submit-order@1.0.0
  with: {object: {reference: {ref: /input/moveId}}}
  irreversible: true
`;
export const NOTIFY_STEP = `- id: notify
  kind: action
  uses: notify.send-message@1.0.0
  with: {object: {text: {ref: /input/moveId}}}
`;
const MANUAL = {
  source: "manual",
  pinnedAt: "2026-11-25T10:00:00Z",
  configHash: "fnv1a64:0000000000000001",
  contractHash: "fnv1a64:0000000000000002",
  upstreamHash: "fnv1a64:0000000000000003",
};
/** Pinned output, as a test data file keeps it. */
export const pinned = (outputs: Record<string, unknown>) =>
  Object.fromEntries(
    Object.entries(outputs).map(([key, output]) => [
      key,
      { output, ...MANUAL },
    ]),
  );
/** What the steps of move return. */
export const MOVED = {
  hold: { holdId: "h-1" },
  add: { id: "a-1" },
  submit: { orderId: "o-1" },
  notify: {},
};
/**
 * Opens the workflow with its test data file on that platform, ready to
 * execute: the Studio host that checks the test data and simulates is the
 * repository's own Python. Returns what Execute sent.
 */
export async function openMoveToExecute(
  page: Page,
  source: string,
  testData: { pins?: object; errors?: object; input?: unknown } = {},
): Promise<{ details: StepDetailsPage; calls: LocalCalls[] }> {
  await withNewEditor(page);
  await connectedCatalog(page);
  const calls = await localAuthoring(page);
  await hostChecksTestData(page);
  const { input = { moveId: "M-1", quantity: 2 }, ...entries } = testData;
  await page.getByLabel("Choose a workflow file").setInputFiles([
    yamlFile("move-between-systems.yaml", source),
    {
      name: "move-between-systems.yaml.testdata.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          schemaVersion: 1,
          kind: "weave.studio/testData",
          updatedAt: "2026-11-25T10:00:00Z",
          input: { value: input, source: "manual" },
          pins: pinned(MOVED),
          ...entries,
        }),
      ),
    },
  ]);
  await page.getByRole("button", { name: "Load", exact: true }).click();
  await expect(
    page.locator(".toast", { hasText: "Loaded the test data" }),
  ).toBeVisible();
  const canvas = page.getByLabel("Workflow canvas", { exact: true });
  const show = page.getByRole("button", { name: "Show canvas", exact: true });
  await expect(canvas.or(show).first()).toBeVisible();
  if (await show.isVisible()) await show.click();
  return { details: new StepDetailsPage(page), calls };
}
/** Execute workflow, from the editor's bar. */
export const executeWorkflow = (page: Page) =>
  page
    .locator(".editor-bar")
    .getByRole("button", { name: "Execute workflow" })
    .click();
/** What the latest execution sent to the Studio host. */
export async function executed(calls: LocalCalls[], count = 1) {
  await expect
    .poll(() => calls.filter(({ path }) => path.endsWith("/execute")).length)
    .toBeGreaterThanOrEqual(count);
  return calls.filter(({ path }) => path.endsWith("/execute")).at(-1)!.body as {
    errors?: Record<string, unknown>;
    mocks: Record<string, unknown>;
  };
}

export interface MoveOptions {
  method?: string;
  features?: readonly string[];
  start?: (page: Page) => Promise<unknown>;
}
export async function openMove(
  page: Page,
  source: string,
  { method, features, start = offline }: MoveOptions = {},
): Promise<StepDetailsPage> {
  await withNewEditor(page);
  await withManifest(page, features);
  await keepMove(page, source, method);
  await start(page);
  return openKept(page);
}

/** Step details on a step inside move, reached from move's as a person does: Next step. */
export async function openInside(details: StepDetailsPage, id: string) {
  if (!(await details.dialog.count())) await details.open("move");
  for (let step = 0; step < 10; step++) {
    if (await details.dialogFor(id).count()) return;
    const shown = await details.dialog.getAttribute("aria-label");
    await details.dialog.getByRole("button", { name: /^Next step/ }).click();
    await expect(details.dialog).not.toHaveAttribute("aria-label", shown ?? "");
  }
  await expect(details.dialogFor(id)).toBeVisible();
}
/** The step named, at any depth, as Source holds it. */
export async function stepInSource(
  page: Page,
  details: StepDetailsPage,
  id: string,
): Promise<Record<string, unknown>> {
  if (await details.dialog.count()) await details.close();
  const read = parse(await sourceText(page)) as {
    spec: { steps: Record<string, unknown>[] };
  };
  const find = (value: unknown): Record<string, unknown> | null => {
    if (!value || typeof value !== "object") return null;
    if (!Array.isArray(value) && (value as { id?: unknown }).id === id)
      return value as Record<string, unknown>;
    for (const item of Object.values(value)) {
      const found = find(item);
      if (found) return found;
    }
    return null;
  };
  const found = find(read.spec.steps);
  if (!found) throw new Error(`No step ${id} in the source`);
  return found;
}
/** The Settings control How it's undone chooses with. */
export const undoSelect = (details: StepDetailsPage) =>
  details.field("undo").getByRole("combobox", { name: "How it's undone" });
