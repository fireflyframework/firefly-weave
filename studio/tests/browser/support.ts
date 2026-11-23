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
// Shared route mocks for the integration editor, round-trip and layout specs.
import { spawn, type ChildProcess } from "node:child_process";
import { resolve as resolvePath } from "node:path";
import { createInterface } from "node:readline";
import { python, repository } from "../python-path";
import { expect, Locator, Page } from "@playwright/test";

/** Exercise a native or shared Select through its public keyboard/pointer UI. */
export async function selectChoice(
  field: Locator,
  choice: string | { label?: string; value?: string; index?: number },
) {
  // read-only audit: whether the field is a native select
  if (await field.evaluate((element) => element.tagName === "SELECT")) {
    await field.selectOption(choice);
    return;
  }
  await field.click();
  const id = await field.getAttribute("aria-controls");
  const list = field.page().locator(`[id=${JSON.stringify(id)}]`);
  await expect(list).toBeVisible();
  const options = list.getByRole("option");
  let option: Locator;
  if (typeof choice !== "string" && choice.index !== undefined) {
    option = options.nth(choice.index);
  } else {
    const value = typeof choice === "string" ? choice : choice.value;
    const label = typeof choice === "string" ? choice : choice.label;
    const byValue = list.locator(
      `[role="option"][data-value=${JSON.stringify(value ?? "")} ]`,
    );
    option =
      value !== undefined && (await byValue.count())
        ? byValue
        : list.getByRole("option", { name: label, exact: true });
  }
  await option.click();
  await expect(list).not.toBeVisible();
}

export const profile = {
  name: "Test platform",
  baseUrl: "https://weave.invalid",
  tenantId: "tenant",
  projectId: "project",
  environmentId: "development",
};
export const allCapabilities = [
  "catalog.read",
  "definition.write",
  "definition.publish",
  "release.activate",
  "connection.manage",
  "run.start",
  "run.read",
  "human_task.read",
  "simulate",
];
export const lookupAction = {
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "sql.lookup", version: "1.0.0" },
  spec: {
    implementation: {
      kind: "connector",
      uses: "weave-postgresql@1.0.0",
      action: "read",
    },
    connection: { connector: "weave-postgresql@1.0.0" },
    sideEffect: "read_only",
    timeoutSeconds: 30,
    retry: { maxAttempts: 3, initialDelaySeconds: 1, maxDelaySeconds: 30 },
    inputSchema: {
      type: "object",
      required: ["parameters"],
      properties: {
        parameters: {
          type: "object",
          title: "Parameters",
          required: ["customerId"],
          properties: {
            customerId: { type: "string", title: "Customer ID", minLength: 1 },
            region: { type: "string", title: "Region", enum: ["eu", "us"] },
          },
        },
        limit: { type: "integer", title: "Limit", minimum: 1 },
        mode: { type: "string", title: "Mode", enum: ["fast", "safe"] },
        label: { type: "string", title: "Label" },
        tags: { type: "array", title: "Tags", items: { type: "string" } },
        dryRun: { type: "boolean", title: "Dry run" },
        options: { type: "object", title: "Options" },
      },
    },
    outputSchema: {
      type: "object",
      properties: { rows: { type: "array" } },
    },
  },
};
export const workerAction = {
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "crm.lookup", version: "2.0.0" },
  spec: {
    implementation: {
      kind: "worker",
      taskType: "crm-lookup",
      taskVersion: "1.0.0",
    },
    sideEffect: "idempotent",
    timeoutSeconds: 60,
    inputSchema: {
      type: "object",
      required: ["customer"],
      properties: { customer: { type: "string", title: "Customer" } },
    },
    outputSchema: { type: "object" },
  },
};
export interface CatalogEntry {
  id: string;
  document: typeof lookupAction | typeof workerAction;
}
export const defaultCatalog: CatalogEntry[] = [
  { id: "a1", document: lookupAction },
  { id: "a2", document: workerAction },
];
export interface ConnectedOptions {
  capabilities?: string[];
  catalog?: CatalogEntry[];
  // Milliseconds to hold each successive catalog page request.
  catalogDelays?: number[];
  catalogStatus?: number;
  // Number of initial catalog requests that fail with catalogStatus.
  catalogFailures?: number;
  // Milliseconds to hold the identity answer (a slow platform check).
  identityDelay?: number;
  // Workspace IDs; UUIDs let Studio keep this account's data on this computer.
  tenantId?: string;
  projectId?: string;
  /** The session's environment ID; "development" by default. */
  environmentId?: string;
  /** The environment's name, which the identity then lists with the workspace. */
  environmentName?: string;
}
export interface Recorder {
  catalogPages: number;
  exports: string[];
}
export async function connected(
  page: Page,
  options: ConnectedOptions = {},
): Promise<Recorder> {
  const recorder: Recorder = { catalogPages: 0, exports: [] };
  await routeTestDataCheck(page);
  const capabilities = options.capabilities ?? allCapabilities;
  const catalog = options.catalog ?? defaultCatalog;
  const tenant = options.tenantId ?? profile.tenantId;
  const project = options.projectId ?? profile.projectId;
  const environment = options.environmentId ?? profile.environmentId;
  await page.route("**/studio/session", (r) =>
    r.fulfill({
      json: {
        paired: true,
        csrfToken: "test-csrf",
        version: "1",
        mode: "connected",
        profile: {
          ...profile,
          tenantId: tenant,
          projectId: project,
          environmentId: environment,
        },
      },
    }),
  );
  await page.route("**/studio/api/api/v1/**", (r) =>
    r.fulfill({ json: { items: [], next_cursor: null } }),
  );
  // The last registered matching route wins, so specific routes follow the catch-all.
  await page.route("**/studio/api/api/v1/identity", async (r) => {
    if (options.identityDelay)
      await new Promise((resolve) =>
        setTimeout(resolve, options.identityDelay),
      );
    await r.fulfill({
      json: {
        principal_id: "human",
        kind: "human",
        grants: [
          {
            role: "test",
            scope: {
              tenant_id: tenant,
              project_id: project,
              environment_id: environment,
            },
            resources: [],
            capabilities,
          },
        ],
        workspaces: options.environmentName
          ? [
              {
                id: tenant,
                name: "Acme",
                projects: [
                  {
                    id: project,
                    name: "Billing",
                    environments: [
                      { id: environment, name: options.environmentName },
                    ],
                  },
                ],
              },
            ]
          : [],
        truncated: false,
      },
    });
  });
  await page.route(`**/projects/${project}/actions?*`, async (r) => {
    const delay = options.catalogDelays?.[recorder.catalogPages] ?? 0;
    recorder.catalogPages++;
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    if (
      options.catalogStatus &&
      recorder.catalogPages <= (options.catalogFailures ?? Infinity)
    )
      return r.fulfill({
        status: options.catalogStatus,
        json: { code: "WV-UNAVAILABLE", message: "Catalog unavailable" },
      });
    await r
      .fulfill({
        json: {
          items: catalog.map((entry) => ({
            id: entry.id,
            name: entry.document.metadata.name,
            version: entry.document.metadata.version,
          })),
          next_cursor: null,
        },
      })
      .catch(() => undefined);
  });
  for (const entry of catalog)
    await page.route(
      `**/projects/${project}/actions/${entry.id}/export`,
      (r) => {
        recorder.exports.push(entry.id);
        return r.fulfill({
          json: {
            id: entry.id,
            name: entry.document.metadata.name,
            version: entry.document.metadata.version,
            document: entry.document,
          },
        });
      },
    );
  await page.route("**/studio/local/validate", (r) =>
    r.fulfill({
      json: {
        validationOk: true,
        errorCount: 0,
        diagnostics: [],
        partial: true,
      },
    }),
  );
  await page.goto("/");
  return recorder;
}
/**
 * A run a test answers runs.read with, as Operate › Runs lists it
 * (run_summaries.list): its workflow from its activation, its status, keys and
 * start. The row opens by its workflow name.
 */
export function runSummary(run: Record<string, unknown>) {
  const state = (run["state"] ?? {}) as Record<string, unknown>;
  const activation = (run["activation"] ?? {}) as Record<string, unknown>;
  const name =
    (run["workflow_name"] as string | undefined) ??
    (activation["name"] as string | undefined) ??
    "workflow";
  const started = (run["started_at"] ??
    run["created_at"] ??
    new Date().toISOString()) as string;
  const status = String(state["status"] ?? "running");
  return {
    id: String(run["id"]),
    workflow: {
      name,
      version: String(run["workflow_version"] ?? "1.0.0"),
      definition_version_id: "0f8fad5b-d9cb-469f-a165-70867728950e",
    },
    activation_id: String(
      activation["id"] ?? "1f8fad5b-d9cb-469f-a165-70867728950e",
    ),
    activation: { name, revision: Number(activation["revision"] ?? 1) },
    status,
    paused: !!state["manual_paused"],
    test: false,
    origin: "manual",
    caller: null,
    retried_from_run_id: null,
    started_at: started,
    updated_at: started,
    ended_at: null,
    duration_ms: null,
    business_key: (run["business_key"] as string | undefined) ?? null,
    correlation_key: (run["correlation_key"] as string | undefined) ?? null,
    failed_step: null,
    active_incidents: 0,
    archived: false,
  };
}
/**
 * Answers Operate › Runs from the runs a test answers runs.read with, filtered
 * by status and exact business key as the platform filters them. Returns the
 * list requests it saw.
 */
export async function runSummaries(
  page: Page,
  runs:
    | readonly Record<string, unknown>[]
    | (() => readonly Record<string, unknown>[]),
  queries: URL[] = [],
): Promise<URL[]> {
  await page.route("**/run-summaries?**", (route) => {
    const url = new URL(route.request().url());
    queries.push(url);
    const statuses = url.searchParams.getAll("status");
    const key = url.searchParams.get("business_key");
    const items = (typeof runs === "function" ? runs() : runs)
      .map(runSummary)
      .filter(
        (summary) =>
          (!statuses.length || statuses.includes(summary.status)) &&
          (!key || summary.business_key === key),
      );
    return route.fulfill({ json: { items, next_cursor: null } });
  });
  return queries;
}
/**
 * A design token's color as the browser computes colors ("rgb(r, g, b)"),
 * read from the page at run time so tests follow the token, not a copy of it.
 */
export async function tokenColor(page: Page, token: string): Promise<string> {
  return page.evaluate((name) => {
    // An undefined token would make the probe inherit the body color, so a
    // misspelled name could pass by accident: refuse it instead.
    if (
      getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim() === ""
    )
      throw new Error(`Design token ${name} is not defined on :root`);
    const probe = document.createElement("i");
    probe.style.color = `var(${name})`;
    document.body.append(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  }, token);
}

/**
 * The Studio host's test data check, as a test double: the dev server has no
 * host. Each entry is checked against its own schema the way the host does it
 * for the cases these tests use: the test input against the workflow's input
 * schema, a pin against its action's output schema from the definitions sent;
 * a present value under `x-secret` or `writeOnly` is refused, and a pin whose
 * action schema isn't known is unresolved.
 */
export async function routeTestDataCheck(page: Page) {
  type Json = Record<string, unknown>;
  const secretIn = (schema: unknown, value: unknown): boolean => {
    if (!schema || typeof schema !== "object" || value === undefined)
      return false;
    const s = schema as Json;
    if (s["x-secret"] === true || s["writeOnly"] === true) return true;
    const properties = s["properties"] as Json | undefined;
    if (
      properties &&
      value &&
      typeof value === "object" &&
      !Array.isArray(value)
    )
      return Object.entries(value as Json).some(([key, item]) =>
        secretIn(properties[key], item),
      );
    if (Array.isArray(value) && s["items"])
      return value.some((item) => secretIn(s["items"], item));
    return false;
  };
  const stepsOf = (steps: unknown): Json[] =>
    Array.isArray(steps)
      ? (steps as Json[]).flatMap((step) => [
          step,
          ...stepsOf(step["steps"]),
          ...((step["cases"] as Json[] | undefined) ?? []).flatMap((c) =>
            stepsOf(c["steps"]),
          ),
          ...stepsOf((step["default"] as Json | undefined)?.["steps"]),
          ...stepsOf((step["body"] as Json | undefined)?.["steps"]),
        ])
      : [];
  await page.route("**/studio/local/testdata/check", async (route) => {
    const body = route.request().postDataJSON() as {
      source: string;
      definitions: Json[];
      testData: Json;
    };
    const workflow = JSON.parse(body.source) as Json;
    const spec = (workflow["spec"] ?? {}) as Json;
    const entries: Json[] = [];
    const input = body.testData["input"] as Json | undefined;
    if (input)
      entries.push(
        secretIn(spec["inputSchema"], input["value"])
          ? { path: "/input", ok: false, code: "WV-SCHEMA-SECRET_VALUE" }
          : { path: "/input", ok: true },
      );
    for (const [key, pin] of Object.entries(
      (body.testData["pins"] as Json | undefined) ?? {},
    )) {
      const id = key.split(/[[#~]/)[0];
      const step = stepsOf(spec["steps"]).find((item) => item["id"] === id);
      const definition = body.definitions.find(
        (item) =>
          `${(item["metadata"] as Json)?.["name"]}@${(item["metadata"] as Json)?.["version"]}` ===
          step?.["uses"],
      );
      const schema = (definition?.["spec"] as Json | undefined)?.[
        "outputSchema"
      ];
      const path = `/pins/${key.replace(/~/g, "~0").replace(/\//g, "~1")}`;
      entries.push(
        step?.["kind"] !== "action" || schema === undefined
          ? { path, ok: false, code: "WV-STUDIO-TESTDATA-UNRESOLVED" }
          : secretIn(schema, (pin as Json)["output"])
            ? { path, ok: false, code: "WV-SCHEMA-SECRET_VALUE" }
            : { path, ok: true },
      );
    }
    // Test answers: a signal's payload against its payload fields, a
    // person's form data against the task's form.
    for (const [section, schemaKey, valueKey] of [
      ["signals", "payloadSchema", "payload"],
      ["humans", "formSchema", "data"],
    ] as const)
      for (const [key, entry] of Object.entries(
        (body.testData[section] as Json | undefined) ?? {},
      )) {
        const id = key.split(/[[#~]/)[0];
        const step = stepsOf(spec["steps"]).find((item) => item["id"] === id);
        const path = `/${section}/${key.replace(/~/g, "~0").replace(/\//g, "~1")}`;
        entries.push(
          step && secretIn(step[schemaKey], (entry as Json)[valueKey])
            ? { path, ok: false, code: "WV-SCHEMA-SECRET_VALUE" }
            : { path, ok: true },
        );
      }
    // An AI task's saved turns: each final answer against its result fields.
    const profiles = (spec["llmProfiles"] ?? {}) as Json;
    for (const [key, entry] of Object.entries(
      (body.testData["aiTurns"] as Json | undefined) ?? {},
    )) {
      const id = key.split(/[[#~]/)[0];
      const step = stepsOf(spec["steps"]).find((item) => item["id"] === id);
      const schema = (
        profiles[String(step?.["profile"])] as Json | undefined
      )?.["outputSchema"];
      const turns = ((entry as Json)["turns"] ?? []) as Json[];
      const path = `/aiTurns/${key.replace(/~/g, "~0").replace(/\//g, "~1")}`;
      entries.push(
        turns.some((turn) => "final" in turn && secretIn(schema, turn["final"]))
          ? { path, ok: false, code: "WV-SCHEMA-SECRET_VALUE" }
          : { path, ok: true },
      );
    }
    await route.fulfill({ json: { entries } });
  });
}

/**
 * This spec tests the classic designer, which the new editor replaces; it goes
 * when the classic designer does.
 */
export async function withClassicDesigner(page: Page) {
  await page.addInitScript(() =>
    localStorage.setItem("ui:weave.editorNext", "false"),
  );
}
export async function offline(page: Page) {
  await routeTestDataCheck(page);
  await page.route("**/studio/session", (r) =>
    r.fulfill({
      json: {
        paired: true,
        csrfToken: "test-session",
        version: "1",
        mode: "offline",
        profile: null,
      },
    }),
  );
  await page.route("**/studio/local/validate", (r) =>
    r.fulfill({
      json: {
        validationOk: true,
        errorCount: 0,
        diagnostics: [],
        partial: true,
        artifact: null,
      },
    }),
  );
  await page.goto("/");
}
export async function newWorkflow(page: Page) {
  await page.getByRole("button", { name: "New workflow", exact: true }).click();
  // The designer loads with its first use: wait until it is on screen,
  // its lists rendered too.
  await expect(page.locator(".editor-bar")).toBeVisible();
  // The classic designer draws its palette; the new editor its canvas (or,
  // on a phone, the outline first).
  await expect(
    page.locator(".palette-step, .canvas-v2, .outline").first(),
  ).toBeAttached();
  // Below 600 px a workflow opens on its outline; these specs use the canvas.
  const canvas = page.getByRole("button", { name: "Show canvas" });
  if (await canvas.isVisible()) await canvas.click();
}
// Inserts a step by its label: with the new editor, from the toolbar's Add a
// step; otherwise from the palette, opening its popover on narrow layouts.
// The popover is a modal sheet there: the insert closes it before anything else.
export async function insertStep(page: Page, label: string) {
  const addStep = page
    .getByRole("toolbar", { name: "Workflow commands" })
    .getByRole("button", { name: "Add a step", exact: true });
  if (await addStep.isVisible()) {
    await addStep.click();
    const panel = page.locator(".add-step-panel");
    // The panel focuses its search once it is drawn and bound.
    await expect(panel.getByRole("combobox")).toBeFocused();
    await panel.getByRole("combobox").fill(label);
    await panel
      .getByRole("option", { name: label, exact: true })
      .first()
      .click();
    await expect(panel).toHaveCount(0);
    return;
  }
  await expect(page.locator(".palette-step").first()).toBeAttached();
  const step = page
    .locator(".palette")
    .getByRole("button", { name: label, exact: true });
  if (!(await step.isVisible()))
    await page.getByRole("button", { name: "Insert step" }).click();
  await step.click();
  await expect(page.locator(".palette.popover-visible")).toHaveCount(0);
}
/**
 * Closes a side panel that covers the page as a modal sheet (narrow layouts,
 * zoom) with a real click on the dimmed page around it.
 */
export async function closeSheet(page: Page) {
  const sheet = page.locator('.sheet-modal[aria-modal="true"]');
  if (!(await sheet.count())) return;
  await page.locator(".sheet-scrim").click({ position: { x: 4, y: 4 } });
  await expect(sheet).toHaveCount(0);
}
// Asserts that a real pointer at the control's center reaches the control itself.
export async function expectHitTarget(locator: Locator) {
  await locator.scrollIntoViewIfNeeded();
  const hit = await locator.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const found = document.elementFromPoint(
      box.left + box.width / 2,
      box.top + box.height / 2,
    );
    return found === element || element.contains(found)
      ? "self"
      : `${found?.tagName}.${found?.className}`;
  });
  expect(hit).toBe("self");
}
/** A workflow's views, in the order the tabs show them. */
export type WorkflowView = "Designer" | "Source" | "Outline" | "Runs";
/** At 767 px and below the view tabs fold into one view switcher. */
export const foldsViews = (page: Page): boolean =>
  (page.viewportSize()?.width ?? Infinity) <= 767;
/** The menu button that stands for the view tabs at 767 px and below. */
export const viewSwitcher = (page: Page): Locator =>
  page.getByRole("button", { name: /^Workflow view: / });
/** Shows a view by its tab, or by the view switcher's menu where tabs fold. */
export async function showView(page: Page, view: WorkflowView) {
  if (!foldsViews(page))
    return page.getByRole("tab", { name: view, exact: true }).click();
  await viewSwitcher(page).click();
  await page.getByRole("menuitemradio", { name: view, exact: true }).click();
}
export async function sourceText(page: Page) {
  await closeSheet(page);
  await showView(page, "Source");
  const value = await page
    .getByRole("textbox", { name: "Workflow source" })
    .inputValue();
  await showView(page, "Designer");
  return value;
}
/**
 * Runs a designer command by its name: the toolbar's button when it shows
 * there (the lifecycle's one primary, Validate, Simulate), otherwise the item
 * in the toolbar's "More" menu.
 */
export async function command(page: Page, name: string) {
  const toolbar = page.getByRole("toolbar", { name: "Workflow commands" });
  const button = toolbar.getByRole("button", { name, exact: true });
  // A command that just finished may still be relabelling its button.
  const shown = await button.waitFor({ state: "visible", timeout: 2000 }).then(
    () => true,
    () => false,
  );
  if (shown) {
    // A blocked command is aria-disabled but still takes a click (it says
    // why); press it with the pointer where Playwright would wait instead.
    if ((await button.getAttribute("aria-disabled")) === "true") {
      const box = (await button.boundingBox())!;
      return page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    }
    return button.click();
  }
  await toolbar.getByRole("button", { name: "More", exact: true }).click();
  const item = page.getByRole("menuitem", {
    name: new RegExp(`^${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`),
  });
  // A blocked item is aria-disabled like its button, and still says why when pressed.
  await item.click({
    force: (await item.getAttribute("aria-disabled")) === "true",
  });
}
/** Whether Undo can run: the toolbar's button, or More's item on phones. */
export async function canUndo(page: Page) {
  const toolbar = page.getByRole("toolbar", { name: "Workflow commands" });
  const button = toolbar.getByRole("button", { name: "Undo", exact: true });
  if (await button.isVisible()) return button.isEnabled();
  await toolbar.getByRole("button", { name: "More", exact: true }).click();
  const item = page.getByRole("menuitem", { name: "Undo" });
  const enabled = (await item.getAttribute("aria-disabled")) !== "true";
  await page.keyboard.press("Escape");
  return enabled;
}

/** Add target, then Other's generic registration of a target and its runner. */
export async function registerTarget(page: Page) {
  await page.getByRole("button", { name: "Add target", exact: true }).click();
  await page.getByText("Other", { exact: true }).click();
  await page
    .getByRole("button", { name: "Register target", exact: true })
    .click();
}

/**
 * What a field can read, from the Studio host's own scope_at with the
 * repository's Python: one line of JSON in, one line out. scope_at reads only
 * the question it is given, so one process can answer every test's questions
 * without one answer changing another.
 */
const SCOPE_AT = `
import json, sys, traceback
from firefly_weave.compiler.parser import parse_source
from firefly_weave.compiler.scope import scope_at
for line in iter(sys.stdin.readline, ""):
    try:
        body = json.loads(line)
        document = parse_source(body["source"], format=body["format"]).value
        answer = {"scope": scope_at(document, body["path"]).model_dump(mode="json", by_alias=True)}
    except Exception:
        answer = {"error": traceback.format_exc()}
    print(json.dumps(answer), flush=True)
`;
/** Longer than any answer takes: a process this slow is stuck, so it is stopped. */
const SCOPE_ANSWER_MS = 30_000;

/**
 * One Python process per Playwright worker answers the scope questions, one
 * at a time, so a question doesn't wait for Python to start and import the
 * compiler. A process that ends or stops answering is replaced at the next
 * question, and the process ends with the worker.
 */
class ScopeHost {
  private child: ChildProcess | null = null;
  private waiting: ((reply: string | Error) => void) | null = null;
  private turn: Promise<unknown> = Promise.resolve();

  constructor() {
    process.on("exit", () => this.child?.kill());
  }

  /** The answer to one question, after the questions asked before it. */
  ask(question: unknown): Promise<unknown> {
    const answer = this.turn.then(() => this.answer(question));
    this.turn = answer.catch(() => undefined);
    return answer;
  }

  private answer(question: unknown): Promise<unknown> {
    const child = (this.child ??= this.start());
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.stop(child);
        this.settle(new Error(`No scope answer in ${SCOPE_ANSWER_MS} ms.`));
      }, SCOPE_ANSWER_MS);
      this.waiting = (reply) => {
        clearTimeout(timer);
        this.waiting = null;
        if (reply instanceof Error) return reject(reply);
        const { scope, error } = JSON.parse(reply) as {
          scope?: unknown;
          error?: string;
        };
        if (error === undefined) resolve(scope);
        else reject(new Error(error));
      };
      child.stdin!.write(`${JSON.stringify(question)}\n`);
    });
  }

  private start(): ChildProcess {
    const child = spawn(python, ["-c", SCOPE_AT], {
      cwd: repository,
      env: { ...process.env, PYTHONPATH: resolvePath(repository, "src") },
      stdio: ["pipe", "pipe", "ignore"],
    });
    // Only the current process answers: a stopped one's late line is dropped.
    createInterface({ input: child.stdout! }).on("line", (line) => {
      if (this.child === child) this.settle(line);
    });
    // Writing to a process that just ended fails here; its exit answers.
    child.stdin!.on("error", () => undefined);
    const ended = (why: string) => {
      if (this.child !== child) return;
      this.child = null;
      this.settle(new Error(`The scope process ended: ${why}.`));
    };
    child.on("exit", (code, signal) => ended(String(signal ?? code)));
    child.on("error", (error) => ended(error.message));
    return child;
  }

  private stop(child: ChildProcess) {
    if (this.child === child) this.child = null;
    child.kill();
  }

  private settle(reply: string | Error) {
    this.waiting?.(reply);
  }
}

const scopeHost = new ScopeHost();

export async function hostScope(page: Page) {
  await page.route("**/studio/local/scope", async (route) => {
    const scope = await scopeHost.ask(route.request().postDataJSON() ?? {});
    await route.fulfill({ json: scope }).catch(() => undefined);
  });
}
