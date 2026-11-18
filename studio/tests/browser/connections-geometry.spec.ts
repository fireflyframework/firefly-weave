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
// How the connected Connection field, its side sheet and a real result lay
// out: the select keeps its width, a failed test's pill and Retry stay
// together, the list opens below the field on a phone, and the real result's
// tag never pushes Pin and Edit off the Output row.
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  PRODUCTION,
  bindable,
  connectionSelect,
  crmWorkflow,
  openConnected,
  revisionId,
  routeConnectionForm,
  type ConnectionsState,
} from "./connections-po";
import {
  RUN_ID,
  accepted,
  executeIn,
  routeStepTests,
  showOutput,
  succeeded,
  view,
} from "./step-tests-po";

const options = (page: Page) =>
  page.getByRole("listbox", { name: "Connection options" }).getByRole("option");
const box = async (locator: Locator) => (await locator.boundingBox())!;
const withInput = crmWorkflow(
  {},
  { with: { object: { customerId: { literal: "c-1042" } } } },
);
const failing: ConnectionsState = {
  bindable: {
    [PRODUCTION]: [
      bindable(1),
      bindable(2, { endpoint_origin: "http://legacy.acme.test" }),
    ],
  },
  tests: {
    [revisionId(2)]: { ok: false, code: "failed", reason: "unauthorized" },
  },
};

for (const size of [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "768x1024", width: 768, height: 1024 },
  { tag: "360x740", width: 360, height: 740 },
])
  test.describe(`connection layout at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a failed test keeps its pill and Retry together, the hint and the reason under the field", async ({
      page,
    }) => {
      const { details } = await openConnected(page, failing, withInput);
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await options(page)
        .filter({ hasText: /^acme-crm-2/ })
        .click();
      const field = details.field("connection");
      const pill = field.locator(".connection-pill");
      const retry = field.getByRole("button", { name: "Retry" });
      await expect(pill).toContainText("Test failed");
      const [p, r] = [await box(pill), await box(retry)];
      expect(Math.abs(p.y + p.height / 2 - (r.y + r.height / 2))).toBeLessThan(
        4,
      );
      if (size.width >= 768) {
        // The select keeps most of the row.
        const select = await box(
          field.locator("weave-select.connection-choice"),
        );
        const row = await box(field.locator(".connection-row"));
        expect(select.width).toBeGreaterThan(row.width * 0.55);
      }
      const hint = field.locator(".param-line");
      await expect(hint).toContainText(
        "In Production · slot http · http://legacy.acme.test",
      );
      await expect(hint.locator(".param-line-mark")).toHaveText(
        "Not encrypted",
      );
      const reason = hint.locator(".param-line-detail");
      await expect(reason).toHaveText(
        "legacy.acme.test refused this connection's sign-in.",
      );
      const danger = await page.evaluate(() =>
        getComputedStyle(document.documentElement)
          .getPropertyValue("--danger-ink")
          .trim(),
      );
      const color = await reason.evaluate((e) => getComputedStyle(e).color);
      const probe = await page.evaluate((value) => {
        const e = document.createElement("span");
        e.style.color = value;
        document.body.append(e);
        const c = getComputedStyle(e).color;
        e.remove();
        return c;
      }, danger);
      expect(color).toBe(probe);
    });

    test("Create connection reads as an action, and plain-HTTP addresses are marked", async ({
      page,
    }) => {
      const { details } = await openConnected(page, failing, withInput);
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      const create = options(page).filter({ hasText: "Create connection" });
      await expect(create).toHaveClass(/choice-action/);
      await expect(create.locator("weave-icon")).toHaveCount(1);
      await expect(
        options(page).filter({ hasText: /^acme-crm-2/ }),
      ).toContainText("Not encrypted");
      if (size.width < 768) {
        // On a phone the choices open as a sheet along the bottom edge, with 48 px rows.
        const list = page.getByRole("listbox", { name: "Connection options" });
        await expect(list).toContainText("Connection for crm-lookup");
        const sheet = await box(list);
        expect(sheet.y + sheet.height).toBeGreaterThanOrEqual(size.height - 1);
        expect(sheet.width).toBeGreaterThanOrEqual(size.width - 1);
        expect((await box(create)).height).toBeGreaterThanOrEqual(48);
        // A press above the sheet closes it.
        await page.mouse.click(size.width / 2, 40);
        await expect(list).toBeHidden();
      }
    });

    test("a real result's tag keeps count, Pin and Edit on the Output row", async ({
      page,
    }) => {
      const { details } = await openConnected(
        page,
        { bindable: { [PRODUCTION]: [bindable(1)] } },
        withInput,
      );
      await routeStepTests(page, {
        starts: [{ status: 202, json: accepted }],
        views: [view(), succeeded({ name: "Ana", open: 2 })],
      });
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await options(page)
        .filter({ hasText: /^acme-crm-1/ })
        .click();
      await (await executeIn(details, "Production")).click();
      const output = await showOutput(details);
      const tag = output.locator(".pane-real-tag");
      await expect(tag).toBeVisible();
      await expect(tag).toHaveAccessibleName(new RegExp(RUN_ID));
      await expect(tag.locator("weave-icon")).toHaveCount(1);
      expect(
        await tag
          .locator(".pane-real-run")
          .evaluate((e) => getComputedStyle(e).fontFamily),
      ).toMatch(/mono|Menlo|Consolas/i);
      const t = await box(tag);
      expect(t.height).toBeLessThanOrEqual(28);
      const pin = await box(output.locator(".pane-pin"));
      const edit = await box(output.locator(".pane-edit"));
      for (const control of [pin, edit])
        expect(
          Math.abs(control.y + control.height / 2 - (t.y + t.height / 2)),
        ).toBeLessThan(6);
    });

    test("a real run that ended without output keeps one row of actions under its notice", async ({
      page,
    }) => {
      const { details } = await openConnected(
        page,
        { bindable: { [PRODUCTION]: [bindable(1)] } },
        withInput,
      );
      await routeStepTests(page, {
        starts: [{ status: 202, json: accepted }],
        views: [
          view({
            status: "failed",
            error: {
              code: "WV-STEP-HTTP_STATUS",
              message: "The step failed: HTTP_STATUS.",
            },
            ended_at: "2026-11-17T10:00:02Z",
          }),
        ],
      });
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await options(page)
        .filter({ hasText: /^acme-crm-1/ })
        .click();
      await (await executeIn(details, "Production")).click();
      const output = await showOutput(details);
      const notice = output.locator(".pane-real-notice");
      await expect(notice).toContainText("A failed step returns no output.");
      // Below 1280 px Output is a tab: its content is there before it shows.
      await expect(notice).toBeVisible();
      const execute = output.getByRole("button", { name: "Execute step" });
      const set = output.getByRole("button", { name: "Set test output" });
      const [n, a, b] = [await box(notice), await box(execute), await box(set)];
      expect(Math.abs(a.y - b.y)).toBeLessThan(2);
      expect(a.y).toBeGreaterThanOrEqual(n.y + n.height);
      expect(Math.abs(a.x - n.x)).toBeLessThan(2);
    });

    test("the side sheet says what it's for, starts with no name, and keeps its footer at the bottom", async ({
      page,
    }) => {
      const state: ConnectionsState = { bindable: { [PRODUCTION]: [] } };
      const { details } = await openConnected(page, state, withInput);
      await routeConnectionForm(page, state, []);
      await details.open("crm-lookup");
      await expect(details.dialog).not.toContainText("· http");
      await connectionSelect(details).click();
      await options(page).filter({ hasText: "Create connection" }).click();
      const sheet = page.getByRole("dialog", {
        name: "New API connection",
        exact: true,
      });
      await expect(sheet).toContainText("For crm-lookup · slot http");
      await expect(
        sheet.getByLabel("Connection name", { exact: true }),
      ).toHaveValue("");
      const panel = await box(sheet);
      const footer = await box(
        sheet.getByRole("button", { name: "Create connection" }),
      );
      expect(panel.y + panel.height - (footer.y + footer.height)).toBeLessThan(
        48,
      );
    });
  });
