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
// Create connection and Edit from a step's Connection field: the HTTP
// connection form opens in a side sheet over step details, and what it saves
// becomes the step's connection, tested, without leaving the step.
import { expect, test, type Page } from "@playwright/test";
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
import { sizes } from "./step-details-po";

const options = (page: Page) =>
  page.getByRole("listbox", { name: "Connection options" }).getByRole("option");
const sheet = (page: Page, name: string) =>
  page.getByRole("dialog", { name, exact: true });

async function start(page: Page, state: ConnectionsState) {
  const created: { id: string; body: Record<string, unknown> }[] = [];
  const opened = await openConnected(page, state, crmWorkflow());
  await routeConnectionForm(page, state, created);
  return { ...opened, created };
}

for (const size of sizes)
  test.describe(`the connection sheet at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("Create connection opens the form beside the step, then binds and tests the new connection", async ({
      page,
    }) => {
      const state: ConnectionsState = { bindable: { [PRODUCTION]: [] } };
      const { details, recorder, created } = await start(page, state);
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await expect(options(page).first()).toHaveText(
        "No weave-http connections you can use in Production.",
      );
      await options(page).filter({ hasText: "Create connection" }).click();
      const form = sheet(page, "New API connection");
      await expect(form).toBeVisible();
      // Step details stay open underneath.
      await expect(details.dialog).toBeAttached();
      await expect(
        form.getByLabel("Connection name", { exact: true }),
      ).toHaveValue("http");
      await form
        .getByLabel("API address", { exact: true })
        .fill("https://api.acme.test");
      const create = form.getByRole("button", { name: "Create connection" });
      await create.scrollIntoViewIfNeeded();
      await create.click();
      await expect(form).toHaveCount(0);
      expect(created).toHaveLength(1);
      await expect(connectionSelect(details)).toHaveValue("http");
      await expect(details.field("connection")).toContainText(
        "Tested just now",
      );
      expect(recorder.tests).toEqual([created[0].id]);
      await expect(details.field("connection")).toContainText(
        "In Production · slot http · https://api.acme.test",
      );
    });

    test("Edit saves a new revision and tests it", async ({ page }) => {
      const state: ConnectionsState = {
        bindable: { [PRODUCTION]: [bindable(1)] },
      };
      const { details, recorder, created } = await start(page, state);
      await page.route(`**/connections/${revisionId(1)}`, (route) =>
        route.fulfill({
          json: {
            id: revisionId(1),
            name: "acme-crm-1",
            revision: 1,
            config: {
              baseUrl: "https://api1.acme.test",
              auth: { kind: "api-key", header: "X-Api-Key" },
            },
            secretRef: { api_key: "acme-api-key" },
            allowed_destinations: ["https://api1.acme.test"],
          },
        }),
      );
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await options(page)
        .filter({ hasText: /^acme-crm-1/ })
        .click();
      await expect(details.field("connection")).toContainText(
        "Tested just now",
      );
      await details
        .field("connection")
        .getByRole("button", { name: "Edit" })
        .click();
      const form = sheet(page, "Edit acme-crm-1");
      await expect(form).toBeVisible();
      await expect(form.getByLabel("API address", { exact: true })).toHaveValue(
        "https://api1.acme.test",
      );
      await expect(
        form.getByLabel("Connection name", { exact: true }),
      ).toHaveValue("acme-crm-1");
      await form
        .getByLabel("API address", { exact: true })
        .fill("https://eu.acme.test");
      const create = form.getByRole("button", { name: "Create connection" });
      await create.scrollIntoViewIfNeeded();
      await create.click();
      await expect(form).toHaveCount(0);
      expect(created).toHaveLength(1);
      expect(created[0].body).toMatchObject({
        name: "acme-crm-1",
        config: { baseUrl: "https://eu.acme.test" },
        secretRef: { api_key: "acme-api-key" },
      });
      await expect(details.field("connection")).toContainText(
        "Tested just now",
      );
      expect(recorder.tests).toEqual([revisionId(1), created[0].id]);
    });

    test("Escape closes the sheet and returns to the field", async ({
      page,
    }) => {
      const { details } = await start(page, {
        bindable: { [PRODUCTION]: [bindable(1)] },
      });
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await options(page).filter({ hasText: "Create connection" }).click();
      const form = sheet(page, "New API connection");
      await expect(form).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(form).toHaveCount(0);
      await expect(connectionSelect(details)).toBeFocused();
      await expect(details.dialog).toBeVisible();
    });

    test("the form warns, without blocking, when the address isn't encrypted", async ({
      page,
    }) => {
      const state: ConnectionsState = { bindable: { [PRODUCTION]: [] } };
      const { details } = await start(page, state);
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await options(page).filter({ hasText: "Create connection" }).click();
      const form = sheet(page, "New API connection");
      await form
        .getByLabel("API address", { exact: true })
        .fill("http://api.acme.test");
      await expect(form.locator(".plain-http")).toContainText("Not encrypted");
      const create = form.getByRole("button", { name: "Create connection" });
      await create.scrollIntoViewIfNeeded();
      await create.click();
      await expect(form).toHaveCount(0);
      await expect(details.field("connection")).toContainText("Not encrypted");
      await expect(details.field("connection")).toContainText(
        "Tested just now",
      );
    });
  });
