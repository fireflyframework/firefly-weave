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
import { hostValidate, platformCompile } from "./local-authoring";
import {
  openStepFixture,
  StepDetailsPage,
  withNewEditor,
  yamlFile,
} from "./step-details-po";
import { connected, offline, sourceText, type CatalogEntry } from "./support";

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
export async function connectedCatalog(page: Page) {
  await connected(page, {
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
  openStepFixture(
    page,
    yamlFile("move-between-systems.yaml", source),
    connectedCatalog,
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
  await details.open("move");
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
