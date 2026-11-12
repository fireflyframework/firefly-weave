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
// Published decision tables on the mocked platform, and the grid that edits
// them in step details.
import { expect, type Locator, type Page } from "@playwright/test";
import { parse } from "yaml";
import { allCapabilities, connected } from "./support";
import { localAuthoring, platformCompile } from "./local-authoring";
import {
  openStepFixture,
  withLanguageFeatures,
  yamlFile,
  type StepDetailsPage,
} from "./step-details-po";

export interface TableDocumentFixture {
  apiVersion: "weave/v1alpha1";
  kind: "DecisionTable";
  metadata: { name: string; version: string };
  spec: Record<string, unknown>;
}

const ref = (name: string) => ({ ref: `/input/${name}` });
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const lit = (value: unknown) => ({ literal: value });

/** payment-policy: an amount and a tier in, approve and a limit out, three rules and Otherwise. */
export function paymentPolicy(version = "1.0.0"): TableDocumentFixture {
  return {
    apiVersion: "weave/v1alpha1",
    kind: "DecisionTable",
    metadata: { name: "payment-policy", version },
    spec: {
      inputSchema: {
        type: "object",
        properties: { amount: { type: "number" }, tier: { type: "string" } },
        required: ["amount"],
      },
      outputSchema: {
        type: "object",
        properties: {
          approve: { type: "boolean" },
          limit: { type: "number" },
        },
        required: ["approve"],
      },
      hitPolicy: "first",
      rules: [
        {
          id: "big-gold",
          when: op(
            "and",
            op("gt", ref("amount"), lit(1000)),
            op("eq", ref("tier"), lit("gold")),
          ),
          output: lit({ approve: true, limit: 5000 }),
        },
        {
          id: "small",
          when: op("lte", ref("amount"), lit(1000)),
          output: lit({ approve: true, limit: 1000 }),
        },
        {
          // Not one test per column: the grid shows it as one formula.
          id: "large-or-platinum",
          when: op(
            "or",
            op("gt", ref("amount"), lit(5000)),
            op("eq", ref("tier"), lit("platinum")),
          ),
          output: lit({ approve: false, limit: 0 }),
        },
      ],
      defaultOutput: lit({ approve: false, limit: 0 }),
    },
  };
}

/** payment-policy with twelve rules, one per tier, and Otherwise. */
export function twelveRules(version: string): TableDocumentFixture {
  const base = paymentPolicy(version);
  return {
    ...base,
    spec: {
      ...base.spec,
      rules: Array.from({ length: 12 }, (_, i) => ({
        id: `tier-${i + 1}`,
        when: {
          op: {
            name: "eq",
            args: [{ ref: "/input/tier" }, { literal: `t${i + 1}` }],
          },
        },
        output: { literal: { approve: i % 2 === 0, limit: (i + 1) * 100 } },
      })),
    },
  };
}

/** payment-policy with no Otherwise result. */
export function withoutOtherwise(version: string): TableDocumentFixture {
  const base = paymentPolicy(version);
  const spec = { ...base.spec };
  delete spec["defaultOutput"];
  return { ...base, spec };
}

export interface TableRoutes {
  lists: number;
  exports: string[];
  /** Answers every later list and export with this status (a platform that went away). */
  fail(status: number): void;
}

/** Routes the project's decision tables: the list, and each version's export. */
export async function routeTables(
  page: Page,
  tables: TableDocumentFixture[] = [paymentPolicy()],
  project = "project",
): Promise<TableRoutes> {
  let failing = 0;
  const routes: TableRoutes = {
    lists: 0,
    exports: [],
    fail: (status) => {
      failing = status;
    },
  };
  const rows = tables.map((document, index) => ({
    id: `t${index + 1}`,
    name: document.metadata.name,
    version: document.metadata.version,
    document,
  }));
  const unavailable = { code: "WV-UNAVAILABLE", message: "Unavailable" };
  await page.route(`**/projects/${project}/decision-tables?*`, (route) => {
    routes.lists++;
    if (failing) return route.fulfill({ status: failing, json: unavailable });
    return route.fulfill({
      json: {
        items: rows.map(({ id, name, version }) => ({ id, name, version })),
        next_cursor: null,
      },
    });
  });
  for (const row of rows)
    await page.route(
      `**/projects/${project}/decision-tables/${row.id}/export`,
      (route) => {
        routes.exports.push(`${row.name}@${row.version}`);
        if (failing)
          return route.fulfill({ status: failing, json: unavailable });
        return route.fulfill({
          json: {
            id: row.id,
            name: row.name,
            version: row.version,
            document: row.document,
          },
        });
      },
    );
  return routes;
}

/** The grid in step details: cells by row (from 0; Otherwise last) and column (0 is the rule's name). */
export class DecisionGridPage {
  constructor(readonly page: Page) {}
  get root(): Locator {
    return this.page.locator("weave-decision-grid");
  }
  get grid(): Locator {
    return this.root.getByRole("grid");
  }
  cell(row: number, col: number): Locator {
    return this.root.locator(`[data-cell="${row}:${col}"]`);
  }
  async open() {
    const edit = this.page
      .getByRole("dialog", { name: /^Step details: / })
      .locator('[data-param="table"]')
      .getByRole("button", { name: "Edit table" });
    await expect(edit).not.toHaveAttribute("aria-disabled");
    await edit.click();
    await expect(this.grid).toBeVisible();
  }
  /** Types into a cell the way a person does: select it, Enter, type, Enter. */
  async edit(row: number, col: number, text: string) {
    const cell = this.cell(row, col);
    await cell.scrollIntoViewIfNeeded();
    await cell.click();
    await this.page.keyboard.press("Enter");
    const field = this.root.locator(".dg-cell-input");
    await expect(field).toBeFocused();
    await field.fill(text);
    await this.page.keyboard.press("Enter");
  }
  problems(): Locator {
    return this.root
      .getByRole("region", { name: "Problems" })
      .or(this.root.locator(".dg-problems"));
  }
  async policy(label: "First match" | "Exactly one" | "Collect all") {
    await this.root
      .getByRole("combobox", { name: "When several rules match" })
      .click();
    await this.page.getByRole("option", { name: label, exact: true }).click();
  }
  /** Add rule in the grid's footer: a rule after the last one. */
  async addRule() {
    await this.root
      .getByRole("group", { name: "Rules and columns" })
      .getByRole("button", { name: "Add rule" })
      .click();
  }
  /** The table bar: the table's version and its Draft or Published pill. */
  async expectTable(version: string, status: "Draft" | "Published") {
    await expect(this.root.locator(".dg-version")).toHaveText(version);
    await expect(this.root.locator(".dg-bar .pill")).toHaveText(status);
  }
  async addColumn(side: "input" | "result", name: string, type: string) {
    await this.root
      .getByRole("button", {
        name: side === "input" ? "Add input column" : "Add result column",
      })
      .click();
    const dialog = this.page.getByRole("dialog", {
      name: side === "input" ? "Add input column" : "Add result column",
    });
    await dialog.getByLabel("Name").fill(name);
    await dialog.getByRole("radio", { name: type, exact: true }).check();
    await dialog.getByRole("button", { name: "Add column" }).click();
    await expect(dialog).toHaveCount(0);
  }
  async back() {
    await this.root.getByRole("button", { name: "Back to the step" }).click();
    await expect(this.root).toHaveCount(0);
  }
}

const ordersText = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: { name: orders, version: 1.0.0 }
spec:
  inputSchema:
    type: object
    required: [amount]
    properties:
      amount: { type: number }
      tier: { type: string }
  outputSchema: { type: object }
  steps:
    - id: score
      kind: decisionTable
      uses: payment-policy@1.2.0
      with: { ref: /input }
  output: { literal: {} }
`;
/** A workflow whose one step uses payment-policy@1.2.0. */
export const ordersWorkflow = yamlFile("orders.yaml", ordersText);

/** Opens that workflow connected, its step's details, and the grid on payment-policy 1.2.0. */
export async function openPaymentGrid(
  page: Page,
): Promise<{ details: StepDetailsPage; grid: DecisionGridPage }> {
  const details = await openStepFixture(page, ordersWorkflow, async (p) => {
    const result = await connected(p);
    await withLanguageFeatures(p);
    return result;
  });
  await routeTables(page, [paymentPolicy("1.2.0")]);
  await details.open("score");
  const grid = new DecisionGridPage(page);
  await grid.open();
  return { details, grid };
}

/** payment-policy's next version, a draft kept on this computer before Studio opens. */
export async function keepTableDraft(
  page: Page,
  document: TableDocumentFixture = paymentPolicy("1.2.1"),
) {
  await page.addInitScript(
    (value) =>
      localStorage.setItem("local:weave.tableDraft.payment-policy", value),
    JSON.stringify({
      kind: "draft",
      name: "payment-policy",
      version: document.metadata.version,
      base: "1.2.0",
      document,
      savedAt: "2026-10-27T08:00:00.000Z",
    }),
  );
}

/** The orders workflow on the draft payment-policy@1.2.1. */
export const draftOrdersWorkflow = yamlFile(
  "orders.yaml",
  ordersText.replace("@1.2.0", "@1.2.1"),
);

/** The draft grid, offline, with the Studio host's own check answering. */
export async function openDraftGrid(
  page: Page,
  document?: TableDocumentFixture,
): Promise<{
  details: StepDetailsPage;
  grid: DecisionGridPage;
  panes: TablePanes;
}> {
  await keepTableDraft(page, document);
  const details = await openStepFixture(page, draftOrdersWorkflow);
  await localAuthoring(page);
  await details.open("score");
  const grid = new DecisionGridPage(page);
  await grid.open();
  return { details, grid, panes: new TablePanes(page) };
}

/** While the grid is open: Input holds the test values, Output the result. */
export class TablePanes {
  constructor(readonly page: Page) {}
  get input(): Locator {
    return this.page.locator(".sd-input weave-table-input-pane");
  }
  get output(): Locator {
    return this.page.locator(".sd-output weave-table-output-pane");
  }
  field(name: string): Locator {
    return this.input.getByLabel(name, { exact: true });
  }
  /** Shows a pane: below 1280 px the panes are tabs (Data, or the sheet's). */
  async show(pane: "Input" | "Parameters" | "Output") {
    const region = this.page.locator(
      pane === "Input"
        ? ".sd-input"
        : pane === "Output"
          ? ".sd-output"
          : ".sd-main",
    );
    if (await region.isVisible()) return;
    // From 768 to 1099 px the Data pane starts folded to its rail.
    const rail = this.page.getByRole("button", { name: "Show data" });
    if (pane !== "Parameters" && (await rail.isVisible())) await rail.click();
    if (await region.isVisible()) return;
    await this.page
      .getByRole("tablist", { name: /^(Data|Step details panes)$/ })
      .getByRole("tab", { name: pane, exact: true })
      .click();
    await expect(region).toBeVisible();
  }
  /** Types the test values the way a person does, field by field. */
  async type(values: Record<string, string>) {
    for (const [name, value] of Object.entries(values))
      await this.field(name).fill(value);
  }
  get count(): Locator {
    return this.output.locator(".dg-try-count");
  }
  get cards(): Locator {
    return this.output.locator(".dg-rule-card");
  }
  get result(): Locator {
    return this.output.locator(".dg-result");
  }
  get checked(): Locator {
    return this.output.locator(".pill", { hasText: "Checked by Weave" });
  }
}

/** The table's document as the Table field's viewer shows it. */
export async function viewedTable(page: Page, details: StepDetailsPage) {
  await details.field("table").getByRole("button", { name: "Open" }).click();
  const viewer = page.getByRole("dialog", { name: /^View payment-policy@/ });
  const text = await viewer.locator("pre").innerText();
  await viewer.getByRole("button", { name: "Close" }).click();
  return parse(text) as {
    metadata: { version: string };
    spec: Record<string, unknown> & {
      rules: { id: string; when: unknown; output: unknown }[];
    };
  };
}

const TENANT = "00000000-0000-4000-8000-000000000011";
const PROJECT = "00000000-0000-4000-8000-000000000012";

export interface TablePlatformOptions {
  capabilities?: string[];
  /** The first table publish answers with this status. */
  publishStatus?: number;
  /** Versions someone else published after Studio listed the tables. */
  takenLater?: string[];
}

/** A connected platform whose catalog holds payment-policy 1.2.0; compiles use the repository's compiler. */
export async function tablePlatform(
  page: Page,
  options: TablePlatformOptions = {},
) {
  const tables = new Map<string, TableDocumentFixture>([
    ["payment-policy@1.2.0", paymentPolicy("1.2.0")],
  ]);
  const posts: { path: string; version: string; key: string }[] = [];
  let first = true;
  await localAuthoring(page);
  await connected(page, {
    capabilities: options.capabilities ?? [...allCapabilities, "compile"],
    tenantId: TENANT,
    projectId: PROJECT,
  });
  await withLanguageFeatures(page);
  const project = `**/projects/${PROJECT}`;
  await page.route(`${project}/compiler/compile`, async (route) => {
    const body = route.request().postDataJSON() as {
      source: unknown;
      format: string;
    };
    await route.fulfill({
      json: await platformCompile({
        ...body,
        definitions: [...tables.values()],
      }),
    });
  });
  await page.route(`${project}/decision-tables?*`, (route) =>
    route.fulfill({
      json: {
        items: [...tables.values()]
          .filter((t) => !options.takenLater?.includes(t.metadata.version))
          .map((t, i) => ({
            id: `t${i + 1}`,
            name: t.metadata.name,
            version: t.metadata.version,
          })),
        next_cursor: null,
      },
    }),
  );
  await page.route(`${project}/decision-tables/t1/export`, (route) =>
    route.fulfill({ json: { document: tables.get("payment-policy@1.2.0") } }),
  );
  await page.route(`${project}/decision-tables`, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const document = parse(
      (route.request().postDataJSON() as { source: string }).source,
    ) as TableDocumentFixture;
    const { name, version } = document.metadata;
    posts.push({
      path: "decision-tables",
      version,
      key: route.request().headers()["idempotency-key"] ?? "",
    });
    if (first && options.publishStatus) {
      first = false;
      return route.fulfill({
        status: options.publishStatus,
        json: { code: "WV-DENIED", message: "Not allowed" },
      });
    }
    if (
      options.takenLater?.includes(version) ||
      tables.has(`${name}@${version}`)
    )
      return route.fulfill({
        status: 409,
        json: { code: "WV-VERSION-CONFLICT", message: "Taken" },
      });
    tables.set(`${name}@${version}`, document);
    await route.fulfill({
      status: 201,
      json: { id: `t${tables.size}`, kind: "DecisionTable", name, version },
    });
  });
  await page.route(`${project}/workflows`, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    posts.push({ path: "workflows", version: "", key: "" });
    await route.fulfill({
      status: 201,
      json: {
        id: "11111111-1111-4111-8111-111111111111",
        kind: "Workflow",
        name: "orders",
        version: "1.0.0",
        digest: "0".repeat(64),
      },
    });
  });
  if (options.takenLater)
    for (const version of options.takenLater)
      tables.set(`payment-policy@${version}`, paymentPolicy(version));
  return { posts };
}
