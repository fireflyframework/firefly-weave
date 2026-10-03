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
import { test, expect } from "@playwright/test";
import { parse } from "yaml";
import {
  offline,
  connected,
  newWorkflow,
  sourceText,
  closeSheet,
  lookupAction,
} from "./support";
import { DesignerPage } from "./designer-po";
import { chooseAction } from "./integrations-po";

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test("valid text edits commit live as one undo each without inspector actions", async ({
      page,
    }) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text());
      });
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("wait");
      await designer.selectStep("wait-1");
      await expect(
        designer.inspector.getByRole("button", {
          name: "Apply changes",
          exact: true,
        }),
      ).toHaveCount(0);
      await expect(
        designer.inspector.getByRole("button", {
          name: "Discard",
          exact: true,
        }),
      ).toHaveCount(0);
      const duration = designer.inspector.getByLabel("Duration", {
        exact: true,
      });
      await duration.fill("2");
      await duration.pressSequentially("3", { delay: 30 });
      await expect(
        designer.node("wait-1").locator(".node-summary"),
      ).toContainText("23 min");
      await expect(duration).toBeFocused();
      await duration.press("ControlOrMeta+z");
      await expect(duration).toHaveValue("1");
      await duration.fill("4");
      await expect(
        designer.node("wait-1").locator(".node-summary"),
      ).toContainText("4 min");
      await designer.inspector.getByLabel("Step name").fill("pause");
      await expect(designer.node("pause")).toHaveCount(1);
      await designer.inspector.getByLabel("Step name").press("ControlOrMeta+z");
      await expect(designer.node("wait-1")).toHaveCount(1);
      expect(parse(await sourceText(page)).spec.steps[0].durationSeconds).toBe(
        240,
      );
      expect(errors).toEqual([]);
    });
    test("invalid edits stay inline and navigation uses the last valid source with Go back", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("wait");
      await expect(designer.node("wait-1")).toHaveCount(1);
      await closeSheet(page);
      await designer.append("fail");
      await designer.selectStep("wait-1");
      await designer.inspector.getByLabel("Step name").fill("bad name");
      await designer.inspector.getByLabel("Duration", { exact: true }).click();
      await expect(designer.inspector.locator("#rename-error")).toBeVisible();
      await expect(designer.inspector.getByLabel("Step name")).toHaveValue(
        "bad name",
      );
      await designer.inspector
        .getByLabel("Duration", { exact: true })
        .fill("0");
      await designer.inspector.getByRole("heading").click();
      await expect(
        designer.inspector.locator('[data-field="durationSeconds"]'),
      ).toContainText("Enter a positive number of minutes.");
      await closeSheet(page);
      await designer.selectStep("fail-1");
      await expect(
        page.getByRole("dialog", { name: "Apply your changes?" }),
      ).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: "Go back", exact: true }),
      ).toBeVisible();
      await page.getByRole("button", { name: "Go back", exact: true }).click();
      await expect(designer.inspector.getByLabel("Step name")).toHaveValue(
        "wait-1",
      );
      const source = parse(await sourceText(page));
      expect(source.spec.steps[0].id).toBe("wait-1");
      expect(source.spec.steps[0].durationSeconds).toBe(60);
      await closeSheet(page);
      await page
        .getByRole("toolbar", { name: "Workflow commands" })
        .getByRole("button", { name: "Validate", exact: true })
        .click();
      await expect(
        page.getByRole("dialog", { name: "Apply your changes?" }),
      ).toHaveCount(0);
    });
    test("a valid sibling commits while another field retains its invalid draft", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("fail");
      await designer.selectStep("fail-1");
      const code = designer.inspector.getByLabel("Error code", { exact: true });
      await code.fill("bad code");
      await designer.inspector
        .getByLabel("Message", { exact: true })
        .fill("Payment rejected");
      await expect(page.locator(".status-chip")).toHaveText(/Draft saved/);
      await expect(code).toHaveValue("bad code");
      await expect(
        designer.inspector.locator('[data-field="code"]'),
      ).toContainText("Use letters");
      const source = parse(await sourceText(page));
      expect(source.spec.steps[0].code).not.toBe("bad code");
      expect(source.spec.steps[0].message).toBe("Payment rejected");
    });
    test("choosing an action updates the graph before filling its required inputs", async ({
      page,
    }) => {
      await connected(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("action");
      await designer.selectStep("call-action-1");
      await chooseAction(page, "sql.lookup@1.0.0");
      await expect(designer.node("call-action-1")).toContainText("sql.lookup");
      await expect(designer.node("call-action-1")).not.toContainText(
        "Choose an action",
      );
      expect(parse(await sourceText(page)).spec.steps[0].uses).toBe(
        "sql.lookup@1.0.0",
      );
      await page.getByRole("tab", { name: "Source", exact: true }).click();
      await expect(
        page.getByRole("button", { name: "Apply changes", exact: true }),
      ).toBeVisible();
    });
    test("one Undo restores an action choice and its automatic defaults", async ({
      page,
    }) => {
      const action = structuredClone(lookupAction);
      action.spec.inputSchema.required = ["dryRun"];
      await connected(page, { catalog: [{ id: "a1", document: action }] });
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("action");
      await designer.selectStep("call-action-1");
      await chooseAction(page, "sql.lookup@1.0.0");
      await expect(page.getByLabel(/^Dry run/)).not.toBeChecked();
      expect(
        parse(await sourceText(page)).spec.steps[0].with.literal.dryRun,
      ).toBe(false);
      await designer.selectStep("call-action-1");
      await designer.inspector.getByLabel("Step name").press("ControlOrMeta+z");
      await expect(designer.node("call-action-1")).toContainText(
        "Choose an action",
      );
      const step = parse(await sourceText(page)).spec.steps[0];
      expect(step.uses).toBe("");
      expect(step.with).toEqual({ literal: {} });
    });
  });
}
