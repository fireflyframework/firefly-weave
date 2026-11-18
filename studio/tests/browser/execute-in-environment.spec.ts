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
// Execute in local from step details: one step of the saved draft runs for
// real as a test run on the platform, asking first when it changes data. The
// platform's step tests are scripted by step-tests-po.ts.
import { expect, test, type Page } from "@playwright/test";
import {
  HTTP,
  PRODUCTION,
  bindable,
  connectionSelect,
  crmWorkflow,
  openConnected,
  revisionId,
  type ConnectionsState,
} from "./connections-po";
import {
  RUN_ID,
  accepted,
  executeIn,
  output,
  refusal,
  routeStepTests,
  sent,
  showOutput,
  succeeded,
  view,
  type StepTestScript,
} from "./step-tests-po";
import { sizes, type StepDetailsPage } from "./step-details-po";

const options = (page: Page) =>
  page.getByRole("listbox", { name: "Connection options" }).getByRole("option");

/** Studio connected to Production with the step's connection chosen. */
async function ready(
  page: Page,
  script: StepTestScript,
  state: Partial<ConnectionsState> = {},
  file = crmWorkflow(
    {},
    { with: { object: { customerId: { literal: "c-1042" } } } },
  ),
) {
  const opened = await openConnected(
    page,
    { bindable: { [PRODUCTION]: [bindable(1)] }, ...state },
    file,
  );
  const recorder = await routeStepTests(page, script);
  const { details } = opened;
  await details.open("crm-lookup");
  await connectionSelect(details).click();
  await options(page)
    .filter({ hasText: /^acme-crm-1/ })
    .click();
  await expect(details.field("connection")).toContainText("Tested just now");
  return { ...opened, steps: recorder };
}
const ok: StepTestScript = {
  starts: [{ status: 202, json: accepted }],
  views: [view(), succeeded({ name: "Ana", open: 2 })],
};
const body = (request: { postDataJSON(): unknown }) =>
  request.postDataJSON() as Record<string, unknown>;
const key = (request: { headers(): Record<string, string> }) =>
  request.headers()["idempotency-key"];
async function run(details: StepDetailsPage) {
  await (await executeIn(details, "Production")).click();
}

for (const size of sizes)
  test.describe(`Execute in local at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a read-only step runs at once and shows its output", async ({
      page,
    }) => {
      let release!: () => void;
      const holdReads = new Promise<void>((resolve) => (release = resolve));
      const { details, steps } = await ready(page, { ...ok, holdReads });
      const item = await executeIn(details, "Production");
      await expect(item).toContainText(
        "Runs this step for real, as a test run",
      );
      await item.click();
      await sent(steps, 1);
      const request = steps.posts[0];
      expect(key(request)).toMatch(/^[0-9a-f-]{36}$/);
      expect(body(request)).toMatchObject({
        kind: "action",
        step_id: "crm-lookup",
        uses: "acme.crm-lookup@1.0.0",
        connections: { http: revisionId(1) },
        releases: {},
        timeout_seconds: 30,
      });
      expect(body(request)["draft_revision"]).toBeGreaterThanOrEqual(1);
      expect(body(request)["acknowledged_side_effect"] ?? null).toBeNull();
      await expect(output(details)).toContainText(
        /Executing crm-lookup in Production · \d+ s/,
      );
      await expect(
        details.dialog.locator('.sd-execute button[title="Stop executing"]'),
      ).toBeVisible();
      await details.close();
      const shape = page.locator(
        '.tile-node[data-tile="crm-lookup"] .tile-shape',
      );
      await expect(shape).toHaveClass(/\blive\b/);
      release();
      await details.open("crm-lookup");
      await expect(output(details)).toContainText(
        `From Production · run ${RUN_ID.slice(0, 8)}`,
      );
      await expect(output(details)).toContainText("Ana");
      await expect(shape).not.toHaveClass(/\blive\b/);
    });

    test("a step that changes data asks first, and Cancel sends nothing", async ({
      page,
    }) => {
      const { details, steps } = await ready(page, ok, {
        sideEffect: "non_idempotent",
      });
      await run(details);
      const question = page.getByRole("alertdialog", {
        name: "Execute crm-lookup in Production?",
      });
      await expect(question).toContainText(
        "crm-lookup changes data in Production. Repeating it repeats the change.",
      );
      // Focus starts on Cancel: a held or repeated Enter changes nothing.
      await expect(
        question.getByRole("button", { name: "Cancel" }),
      ).toBeFocused();
      await expect(
        question.getByRole("button", { name: "Run it" }),
      ).toHaveClass(/primary/);
      await page.keyboard.press("Enter");
      await expect(question).toHaveCount(0);
      // Run it afterwards: exactly one step test reaches the platform, so
      // the canceled click sent none, not even late.
      await run(details);
      await question.getByRole("button", { name: "Run it" }).click();
      await expect(output(details)).toContainText("Ana");
      expect(steps.posts).toHaveLength(1);
    });

    test("Run it sends the acknowledgement, and each click asks again", async ({
      page,
    }) => {
      const { details, steps } = await ready(page, ok, {
        sideEffect: "non_idempotent",
      });
      await run(details);
      const question = page.getByRole("alertdialog", {
        name: "Execute crm-lookup in Production?",
      });
      await question.getByRole("button", { name: "Run it" }).click();
      await sent(steps, 1);
      expect(body(steps.posts[0])["acknowledged_side_effect"]).toBe(
        "non_idempotent",
      );
      await expect(output(details)).toContainText("Ana");
      await run(details);
      await expect(question).toBeVisible();
      await question.getByRole("button", { name: "Cancel" }).click();
      expect(steps.posts).toHaveLength(1);
    });

    test("the platform's own confirmation is honored with a new key", async ({
      page,
    }) => {
      const { details, steps } = await ready(page, {
        starts: [
          refusal(409, "WV-STEPTEST-CONFIRM", {
            side_effect: "idempotency_key",
            action: "acme.crm-lookup@1.0.0",
            environment: "Production",
          }),
          { status: 202, json: accepted },
        ],
        views: ok.views,
      });
      await run(details);
      const question = page.getByRole("alertdialog", {
        name: "Execute crm-lookup in Production?",
      });
      await question.getByRole("button", { name: "Run it" }).click();
      await sent(steps, 2);
      expect(key(steps.posts[1])).not.toBe(key(steps.posts[0]));
      expect(body(steps.posts[1])["acknowledged_side_effect"]).toBe(
        "idempotency_key",
      );
      await expect(output(details)).toContainText("Ana");
    });

    test("a release question lets you choose", async ({ page }) => {
      const { details, steps } = await ready(page, {
        starts: [
          refusal(422, "WV-STEPTEST-RELEASE", {
            choices: {
              "weave-http@2.0.0": [
                { id: "r-old", label: "weave-http 2.0.0 (image a1b2)" },
                { id: "r-new", label: "weave-http 2.0.0 (image c3d4)" },
              ],
            },
          }),
          { status: 202, json: accepted },
        ],
        views: ok.views,
      });
      await run(details);
      const question = page.getByRole("alertdialog", {
        name: "Choose a release",
      });
      await expect(question).toContainText(
        "Choose which release runs crm-lookup in Production.",
      );
      await question.getByRole("combobox").selectOption("r-new");
      await question.getByRole("button", { name: "Run it" }).click();
      await sent(steps, 2);
      expect(body(steps.posts[1])["releases"]).toEqual({
        "weave-http@2.0.0": "r-new",
      });
    });

    test("no release that can run the step says so instead of asking", async ({
      page,
    }) => {
      const { details } = await ready(page, {
        starts: [
          {
            status: 422,
            json: {
              code: "WV-STEPTEST-RELEASE",
              message: "No release in this environment runs weave-http@2.0.0.",
              result: { choices: { "weave-http@2.0.0": [] } },
            },
          },
        ],
        views: ok.views,
      });
      await run(details);
      await expect(output(details)).toContainText(
        "No release in this environment runs weave-http@2.0.0.",
      );
      await expect(
        page.getByRole("alertdialog", { name: "Choose a release" }),
      ).toHaveCount(0);
    });

    test("a connection problem names the slot and Fix opens the dropdown", async ({
      page,
    }) => {
      const { details } = await ready(page, {
        starts: [
          refusal(422, "WV-STEPTEST-CONNECTION", {
            slots: {
              http: "The secret handle acme-api-key isn't available in Production.",
            },
          }),
        ],
        views: ok.views,
      });
      await run(details);
      await expect(output(details)).toContainText(
        "The secret handle acme-api-key isn't available in Production.",
      );
      await (await showOutput(details))
        .getByRole("button", { name: "Fix" })
        .click();
      await expect(connectionSelect(details)).toBeFocused();
      await expect(options(page).first()).toBeVisible();
    });

    test("a missing connection shows Setup needed only after trying", async ({
      page,
    }) => {
      const opened = await openConnected(
        page,
        { bindable: { [PRODUCTION]: [bindable(1), bindable(2)] } },
        crmWorkflow({ crm: HTTP }, { connection: "crm" }),
      );
      const steps = await routeStepTests(page, ok);
      const { details } = opened;
      await expect(details.node("crm-lookup")).not.toContainText(
        "choose a connection for slot crm",
      );
      await details.open("crm-lookup");
      await run(details);
      await expect(output(details)).toContainText(
        "Choose a connection for slot crm to execute crm-lookup in Production.",
      );
      expect(steps.posts).toHaveLength(0);
      await details.close();
      await expect(
        page.getByRole("button", { name: /^crm-lookup,/ }),
      ).toHaveAccessibleName(/Setup needed: choose a connection for slot crm/);
    });

    test("Stop cancels the step test", async ({ page }) => {
      let release!: () => void;
      const holdReads = new Promise<void>((resolve) => (release = resolve));
      const { details, steps } = await ready(page, {
        ...ok,
        views: [view()],
        holdReads,
      });
      await run(details);
      await expect(output(details)).toContainText("Executing crm-lookup");
      await (await showOutput(details))
        .getByRole("button", { name: "Stop", exact: true })
        .click();
      await expect.poll(() => steps.cancels).toBe(1);
      release();
      await expect(output(details)).toContainText("Canceled.");
    });

    test("too many at once says to wait", async ({ page }) => {
      const { details } = await ready(page, {
        starts: [
          refusal(429, "WV-STEPTEST-BUSY", undefined, { "Retry-After": "3" }),
        ],
        views: ok.views,
      });
      await run(details);
      await expect(output(details)).toContainText(
        "You already have 4 steps executing in Production. Try again when one finishes.",
      );
    });

    test("test calls off says where to go", async ({ page }) => {
      const { details } = await openConnected(
        page,
        {
          bindable: { [PRODUCTION]: [bindable(1)] },
          policy: { [PRODUCTION]: "disabled" },
        },
        crmWorkflow(),
      );
      await details.open("crm-lookup");
      const item = await executeIn(details, "Production");
      await expect(item).toBeDisabled();
      await expect(item).toContainText(
        "Test calls are off in Production. Choose an environment where they're on to execute steps there.",
      );
    });

    test("upstream data missing offers Execute previous steps", async ({
      page,
    }) => {
      const { details, steps } = await ready(page, ok, {}, crmWorkflow());
      await run(details);
      await expect(output(details)).toContainText(
        "crm-lookup needs data from earlier steps. Execute previous steps first.",
      );
      await expect(
        (await showOutput(details)).getByRole("button", {
          name: "Execute previous steps",
        }),
      ).toBeVisible();
      expect(steps.posts).toHaveLength(0);
    });
  });
