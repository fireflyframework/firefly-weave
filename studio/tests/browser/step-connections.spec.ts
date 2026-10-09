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
import { expect, test, type Locator } from "@playwright/test";
import { parse, stringify } from "yaml";
import { readFileSync } from "node:fs";
import {
  openConnectedFixture,
  openStepFixture,
  sizes,
  stepFixture,
  yamlFile,
  withNewEditor,
  StepDetailsPage,
} from "./step-details-po";
import { sourceText, connected, allCapabilities } from "./support";

async function usable(control: Locator) {
  await control.scrollIntoViewIfNeeded();
  const geometry = await control.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {
      width: rect.width,
      height: rect.height,
      contained:
        rect.left >= 0 &&
        rect.right <= innerWidth &&
        rect.top >= 0 &&
        rect.bottom <= innerHeight,
      hit: element.contains(
        document.elementFromPoint(
          rect.x + rect.width / 2,
          rect.y + rect.height / 2,
        ),
      ),
    };
  });
  expect(geometry).toMatchObject({ contained: true, hit: true });
  expect(geometry.width).toBeGreaterThanOrEqual(44);
  expect(geometry.height).toBeGreaterThanOrEqual(44);
}

for (const size of [
  ...sizes,
  { tag: "360x640", width: 360, height: 640 },
  { tag: "640x360", width: 640, height: 360 },
])
  test.describe(`resources and connections at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a resource field offers From list, By name and Open", async ({
      page,
    }) => {
      const details = await openConnectedFixture(page);
      await details.open("lookup");
      const action = details.field("action");
      const modes = action.getByRole("radiogroup", { name: "Mode for Action" });
      await expect(
        modes.getByRole("radio", { name: "From list" }),
      ).toHaveAttribute("aria-checked", "true");
      await expect(
        action.getByRole("combobox", { name: "Action" }),
      ).toHaveAttribute("data-value", "sql.lookup@1.0.0");
      await usable(
        action.getByRole("button", { name: "Show choices for Action" }),
      );
      await usable(action.getByRole("button", { name: "Open" }));
      await action.getByRole("button", { name: "Open" }).click();
      const viewer = page.getByRole("dialog", {
        name: "View sql.lookup@1.0.0",
      });
      await expect(viewer).toContainText("sideEffect: read_only");
      await viewer.getByRole("button", { name: "Close", exact: true }).focus();
      await page.keyboard.press("Shift+Tab");
      await expect(
        viewer.getByRole("button", { name: "Copy", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(
        viewer.getByRole("button", { name: "Close", exact: true }),
      ).toBeFocused();
      await usable(viewer.getByRole("button", { name: "Copy", exact: true }));
      await usable(viewer.getByRole("button", { name: "Close", exact: true }));
      await page.screenshot({
        path: test.info().outputPath(`resource-viewer-${size.tag}.png`),
      });
      await page.keyboard.press("Escape");
      await expect(viewer).toHaveCount(0);
      await expect(action.getByRole("button", { name: "Open" })).toBeFocused();
      await expect(details.dialog).toBeVisible();
      const listMode = modes.getByRole("radio", { name: "From list" });
      await usable(listMode);
      await listMode.focus();
      await page.keyboard.press("ArrowRight");
      await expect(modes.getByRole("radio", { name: "By name" })).toBeFocused();
      await expect(
        modes.getByRole("radio", { name: "By name" }),
      ).toHaveAttribute("aria-checked", "true");
      const name = action.getByRole("textbox", { name: "Action" });
      await expect(name).toHaveValue("sql.lookup@1.0.0");
      await name.fill("lookup");
      await name.press("Enter");
      await expect(action.getByRole("alert")).toHaveText(
        "Use name@1.2.0, such as orders.get@1.0.0.",
      );
      await listMode.click();
      await modes.getByRole("radio", { name: "By name" }).click();
      await expect(name).toHaveValue("lookup");
      await page.screenshot({
        path: test.info().outputPath(`resource-draft-${size.tag}.png`),
      });
      await name.fill("orders.get@1.2.3-rc.1+build.7");
      await name.press("Enter");
      await expect(action.getByRole("alert")).toHaveCount(0);
      await details.dialog
        .getByRole("button", { name: "Rename lookup" })
        .focus();
      await page.keyboard.press("ControlOrMeta+z");
      await expect(name).toHaveValue("sql.lookup@1.0.0");
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(size.width);
      await details.close();
      expect(parse(await sourceText(page)).spec.steps[1].uses).toBe(
        "sql.lookup@1.0.0",
      );
    });

    test("the Connection field lists slots and New slot, and renames a slot from its menu", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      const connection = details.field("connection");
      await expect(connection).toContainText(
        "Choose the real connection when you activate, or connect to a platform to test.",
      );
      const select = connection.getByRole("combobox", { name: "Connection" });
      await expect(select).toHaveAttribute("data-value", "ledger-db");
      await usable(
        connection.getByRole("button", { name: "Field menu for Connection" }),
      );
      await usable(
        connection.getByRole("button", { name: "Show choices for Connection" }),
      );
      await select.click();
      await expect(
        page.getByRole("option", { name: /^ledger-db/ }),
      ).toBeVisible();
      await expect(
        page.getByRole("option", { name: /^Create connection/ }),
      ).toHaveCount(0);
      await page.getByRole("option", { name: /^New slot/ }).click();
      await expect(select).toHaveAttribute("data-value", "postgresql");
      await details.dialog
        .getByRole("button", { name: "Rename lookup" })
        .focus();
      await page.keyboard.press("ControlOrMeta+z");
      await expect(select).toHaveAttribute("data-value", "ledger-db");
      await connection
        .getByRole("button", { name: "Field menu for Connection" })
        .click();
      await page.getByRole("menuitem", { name: /Rename slot/ }).click();
      const rename = page.getByRole("dialog", {
        name: "Rename slot ledger-db",
      });
      await expect(rename).toBeVisible();
      await expect(rename.getByLabel("Slot name")).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(rename).toHaveCount(0);
      await expect(
        connection.getByRole("button", { name: "Field menu for Connection" }),
      ).toBeFocused();
      await expect(select).toHaveAttribute("data-value", "ledger-db");
      await connection
        .getByRole("button", { name: "Field menu for Connection" })
        .click();
      await page.getByRole("menuitem", { name: /Rename slot/ }).click();
      await rename.getByLabel("Slot name").fill("orders-db");
      await rename.getByRole("button", { name: "Rename" }).click();
      await expect(select).toHaveAttribute("data-value", "orders-db");
      await connection
        .getByRole("button", { name: "Field menu for Connection" })
        .click();
      await page.getByRole("menuitem", { name: /Use another slot/ }).click();
      await expect(select).toBeFocused();
      await expect(select).toHaveAttribute("aria-expanded", "true");
      await page.screenshot({
        path: test.info().outputPath(`connection-slots-${size.tag}.png`),
      });
      await page.keyboard.press("Escape");
      await expect(details.dialog).toBeVisible();
      await details.close();
      const document = parse(await sourceText(page));
      expect(Object.keys(document.spec.connections)).toEqual(["orders-db"]);
      expect(document.spec.steps[1].connection).toBe("orders-db");
    });
  });

for (const size of sizes)
  test.describe(`resource authority at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("unknown resource values and declared connector mismatches stay visible", async ({
      page,
    }) => {
      const workflow = parse(readFileSync(stepFixture, "utf8"));
      workflow.spec.connections["ledger-db"].connector = "weave-http@1.0.0";
      const details = await openConnectedFixture(
        page,
        yamlFile("mismatch.yaml", stringify(workflow)),
      );
      await details.open("lookup");
      const connection = details.field("connection");
      await connection.getByRole("combobox", { name: "Connection" }).click();
      const option = page.getByRole("option", { name: /^ledger-db/ });
      await expect(option).toContainText("This slot uses weave-http@1.0.0");
      await expect(option).not.toContainText("Not declared");
      await page.keyboard.press("Escape");
      const action = details.field("action");
      await action.getByRole("radio", { name: "By name" }).click();
      const name = action.getByRole("textbox", { name: "Action", exact: true });
      await name.fill("imported.action@1.0.0");
      await name.press("Enter");
      await action.getByRole("radio", { name: "From list" }).click();
      await expect(
        action.getByRole("combobox", { name: "Action" }),
      ).toHaveValue("imported.action@1.0.0");
      await details.close();
      expect(parse(await sourceText(page)).spec.steps[1].uses).toBe(
        "imported.action@1.0.0",
      );
    });
    test("connection setup needs management permission while local slots stay editable", async ({
      page,
    }) => {
      const details = await openStepFixture(page, stepFixture, (p) =>
        connected(p, {
          capabilities: allCapabilities.filter(
            (value) => value !== "connection.manage",
          ),
        }),
      );
      await details.open("lookup");
      const connection = details.field("connection");
      await connection.getByRole("combobox", { name: "Connection" }).click();
      const create = page.getByRole("option", { name: /^Create connection/ });
      await expect(create).toHaveAttribute("aria-disabled", "true");
      await expect(create).toContainText("Ask an administrator");
      await page.getByRole("option", { name: /^New slot/ }).click();
      await expect(
        connection.getByRole("combobox", { name: "Connection" }),
      ).toHaveAttribute("data-value", "postgresql");
    });
    test("authorized connection setup opens for the slot and returns focus when dismissed", async ({
      page,
    }) => {
      const details = await openConnectedFixture(page);
      await details.open("lookup");
      const select = details
        .field("connection")
        .getByRole("combobox", { name: "Connection" });
      await select.click();
      await page.getByRole("option", { name: /^Create connection/ }).click();
      const setup = page.getByRole("dialog", {
        name: "New API connection",
        exact: true,
      });
      await expect(setup).toBeVisible();
      await expect(
        setup.getByLabel("Connection name", { exact: true }),
      ).toHaveValue("ledger-db");
      await page.keyboard.press("Escape");
      await expect(setup).toHaveCount(0);
      await expect(select).toBeFocused();
      await expect(details.dialog).toBeVisible();
      await details.close();
      expect(parse(await sourceText(page)).spec.steps[1].connection).toBe(
        "ledger-db",
      );
    });
    test("table choices distinguish loading, failure and retry without duplicate requests", async ({
      page,
    }) => {
      await withNewEditor(page);
      await connected(page);
      await page
        .getByLabel("Choose a workflow file")
        .setInputFiles(stepFixture);
      const details = new StepDetailsPage(page);
      const canvas = page.getByRole("button", {
        name: "Show canvas",
        exact: true,
      });
      if (await canvas.isVisible()) await canvas.click();
      // Selecting the card loads the classic inspector before step details opens.
      await Promise.all([
        page.waitForResponse((response) =>
          response.url().includes("/decision-tables?"),
        ),
        details.node("score").click(),
      ]);
      let requests = 0;
      let release!: () => void;
      const blocked = new Promise<void>((resolve) => {
        release = resolve;
      });
      await page.route(
        "**/projects/project/decision-tables?*",
        async (route) => {
          requests++;
          if (requests === 1) {
            await blocked;
            await route.fulfill({
              status: 503,
              json: { code: "WV-UNAVAILABLE", message: "Unavailable" },
            });
          } else
            await route.fulfill({
              json: {
                items: [{ id: "t1", name: "score", version: "1.0.0" }],
                next_cursor: null,
              },
            });
        },
      );
      await details.openWithKeyboard("score");
      const table = details.field("table");
      await expect(table).toContainText("Loading choices…");
      await table.getByRole("radio", { name: "By name" }).click();
      const name = table.getByRole("textbox", { name: "Table", exact: true });
      await name.fill("incomplete");
      await name.press("Enter");
      expect(requests).toBe(1);
      release();
      await expect(table).toContainText("Couldn't load the list.");
      await table.getByRole("button", { name: "Retry", exact: true }).click();
      await expect(table).not.toContainText("Couldn't load the list.");
      await table.getByRole("radio", { name: "From list" }).click();
      await table.getByRole("radio", { name: "By name" }).click();
      await expect(name).toHaveValue("incomplete");
      expect(requests).toBe(2);
    });
  });
