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
// The Connection field connected to an environment: it lists the connections
// this person may bind there, chooses the slot behind the scenes, keeps the
// choice on this computer (IDs and name only) and tests it. The platform is
// the fake host of platform-host.ts with the connection operations recorded.
import { expect, test, type Page } from "@playwright/test";
import { parse } from "yaml";
import {
  HTTP,
  PRODUCTION,
  STAGING,
  bindable,
  connectionId,
  connectionSelect,
  crmWorkflow,
  keptOnThisComputer,
  openConnected,
  revisionId,
} from "./connections-po";
import { AddStepPanel } from "./add-step-po";
import { START_PLUS, sizes } from "./step-details-po";
import { allCapabilities, sourceText } from "./support";

const definition = async (page: Page) => parse(await sourceText(page));
async function switchTo(page: Page, workspace: string) {
  await page.locator(".platform-indicator").click();
  await page.getByRole("button", { name: "Switch workspace" }).click();
  await page.getByRole("radio", { name: workspace }).check();
  await page.getByRole("button", { name: "Start working" }).click();
}
const options = (page: Page) =>
  page.getByRole("listbox", { name: "Connection options" }).getByRole("option");

for (const size of sizes)
  test.describe(`the Connection field connected at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a step lists the connections you can use here, with their addresses", async ({
      page,
    }) => {
      const { details } = await openConnected(
        page,
        {
          bindable: {
            [PRODUCTION]: [
              bindable(1),
              bindable(2, { enabled: false }),
              bindable(3, { endpoint_origin: undefined }),
            ],
          },
        },
        crmWorkflow(),
      );
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await expect(page.locator(".choice-group")).toHaveText(["In Production"]);
      await expect(options(page)).toHaveCount(4);
      await expect(options(page).nth(0)).toContainText("acme-crm-1");
      await expect(options(page).nth(0)).toContainText(
        "https://api1.acme.test",
      );
      await expect(options(page).nth(1)).toContainText(
        "Turned off in Production",
      );
      await expect(options(page).nth(1)).toHaveAttribute(
        "aria-disabled",
        "true",
      );
      await expect(options(page).nth(2)).toContainText("No address");
      await expect(options(page).nth(3)).toContainText("Create connection");
    });

    test("with one connection, inserting a step chooses it, declares one slot and tests it", async ({
      page,
    }) => {
      const { details, recorder } = await openConnected(
        page,
        { bindable: { [PRODUCTION]: [bindable(1)] } },
        crmWorkflow(),
      );
      await page
        .getByRole("button", { name: START_PLUS })
        .filter({ visible: true })
        .first()
        .click();
      const panel = new AddStepPanel(page);
      await panel.search.fill("acme.crm-lookup");
      await panel.option("acme.crm-lookup").first().click();
      await expect(details.dialogFor("call-action-1")).toBeVisible();
      await expect(connectionSelect(details)).toHaveValue("acme-crm-1");
      await expect(details.field("connection")).toContainText(
        "Tested just now",
      );
      expect(recorder.tests).toEqual([revisionId(1)]);
      await expect(details.field("connection")).toContainText(
        "In Production · slot weave-http · https://api1.acme.test",
      );
      await details.close();
      // The insert declared the slot; choosing the connection reused it.
      const document = await definition(page);
      expect(Object.keys(document.spec.connections)).toEqual(["weave-http"]);
      expect(document.spec.steps[0].connection).toBe("weave-http");
    });

    test("with two, the field stays empty and the step needs setup", async ({
      page,
    }) => {
      const { details, recorder } = await openConnected(
        page,
        { bindable: { [PRODUCTION]: [bindable(1), bindable(2)] } },
        crmWorkflow(),
      );
      await page
        .getByRole("button", { name: START_PLUS })
        .filter({ visible: true })
        .first()
        .click();
      const panel = new AddStepPanel(page);
      await panel.search.fill("acme.crm-lookup");
      await panel.option("acme.crm-lookup").first().click();
      await expect(details.dialogFor("call-action-1")).toBeVisible();
      await expect(details.field("connection")).toContainText(
        "Choose a connection you can use in Production.",
      );
      await expect(connectionSelect(details)).toHaveValue("");
      await expect(
        details.field("connection").locator(".connection-pill"),
      ).toHaveCount(0);
      await details.close();
      expect(recorder.tests).toEqual([]);
      await expect
        .poll(async () => keptOnThisComputer(page))
        .not.toContain("devBindings");
    });

    test("choosing a connection is one undo step", async ({ page }) => {
      const { details } = await openConnected(
        page,
        { bindable: { [PRODUCTION]: [bindable(1), bindable(2)] } },
        crmWorkflow(),
      );
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await options(page)
        .filter({ hasText: /^acme-crm-2/ })
        .click();
      await expect(connectionSelect(details)).toHaveValue("acme-crm-2");
      await expect(details.field("connection")).toContainText(
        "Tested just now",
      );
      await details.close();
      expect((await definition(page)).spec.steps[0].connection).toBe("http");
      await page.keyboard.press("ControlOrMeta+z");
      const undone = await definition(page);
      expect(undone.spec.connections).toBeUndefined();
      expect(undone.spec.steps[0].connection).toBeUndefined();
    });

    test("a failed test says why and Retry tests again", async ({ page }) => {
      const state = {
        bindable: { [PRODUCTION]: [bindable(1)] },
        tests: {
          [revisionId(1)]: {
            ok: false,
            code: "failed",
            reason: "unauthorized",
          },
        } as Record<string, Record<string, unknown>>,
      };
      const { details, recorder } = await openConnected(
        page,
        state,
        crmWorkflow(),
      );
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await options(page)
        .filter({ hasText: /^acme-crm-1/ })
        .click();
      const field = details.field("connection");
      await expect(field).toContainText("Test failed");
      await expect(field).toContainText(
        "api1.acme.test refused this connection's sign-in.",
      );
      // The host, as the connection form says it, not the address.
      await expect(field).not.toContainText("https://api1.acme.test refused");
      state.tests[revisionId(1)] = { ok: true, code: "ok" };
      await field.getByRole("button", { name: "Retry" }).click();
      await expect(field).toContainText("Tested just now");
      await expect(field.getByRole("button", { name: "Retry" })).toHaveCount(0);
      expect(recorder.tests).toEqual([revisionId(1), revisionId(1)]);
    });

    test("without connection management nothing is tested and the hint says so", async ({
      page,
    }) => {
      const { details, recorder } = await openConnected(
        page,
        {
          bindable: { [PRODUCTION]: [bindable(1)] },
          capabilities: [...allCapabilities, "compile"].filter(
            (capability) => capability !== "connection.manage",
          ),
        },
        crmWorkflow(),
      );
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      const create = options(page).filter({ hasText: /^Create connection/ });
      await expect(create).toHaveAttribute("aria-disabled", "true");
      await expect(create).toContainText(
        "Your account can't manage connections in Production. Ask an administrator.",
      );
      await options(page)
        .filter({ hasText: /^acme-crm-1/ })
        .click();
      const field = details.field("connection");
      await expect(field).toContainText(
        "Your account can't test connections in Production.",
      );
      await expect(field.locator(".connection-pill")).toHaveCount(0);
      expect(recorder.tests).toEqual([]);
    });

    test("a connection that's gone says so and is forgotten", async ({
      page,
    }) => {
      const state = { bindable: { [PRODUCTION]: [bindable(1)] } };
      const { details } = await openConnected(
        page,
        state,
        crmWorkflow({ crm: HTTP }, { connection: "crm" }),
      );
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await options(page)
        .filter({ hasText: /^acme-crm-1/ })
        .click();
      await expect(details.field("connection")).toContainText(
        "Tested just now",
      );
      await details.close();
      // The connection is deleted on the platform; Studio asks again after a workspace switch.
      state.bindable[PRODUCTION] = [];
      await switchTo(page, "Acme / Payments / Staging");
      await switchTo(page, "Acme / Payments / Production");
      await details.open("crm-lookup");
      await expect(details.field("connection")).toContainText(
        "acme-crm-1 isn't available in Production anymore. Choose another connection.",
      );
      await expect(connectionSelect(details)).toHaveValue("");
      await expect
        .poll(async () => keptOnThisComputer(page))
        .not.toContain(connectionId(1));
    });

    test("nothing about a connection but its IDs and name is kept on this computer", async ({
      page,
    }) => {
      const { details } = await openConnected(
        page,
        {
          bindable: {
            [PRODUCTION]: [
              bindable(1, { endpoint_origin: "https://secret-host.acme.test" }),
            ],
          },
        },
        crmWorkflow(),
      );
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await options(page)
        .filter({ hasText: /^acme-crm-1/ })
        .click();
      await expect(details.field("connection")).toContainText(
        "Tested just now",
      );
      await expect
        .poll(async () => keptOnThisComputer(page))
        .toContain(revisionId(1));
      const kept = JSON.parse(await keptOnThisComputer(page)) as {
        local: Record<string, string>;
      };
      const [key, text] = Object.entries(kept.local).find(([name]) =>
        name.endsWith("weave.devBindings.v1"),
      )!;
      expect(key).toMatch(/:weave\.devBindings\.v1$/);
      expect(text).not.toContain("secret-host");
      const document = JSON.parse(text) as {
        v: number;
        bindings: Record<string, Record<string, Record<string, unknown[]>>>;
      };
      const row = document.bindings[PRODUCTION]["order-intake"]["http"];
      expect(row.slice(0, 3)).toEqual([
        connectionId(1),
        revisionId(1),
        "acme-crm-1",
      ]);
      expect(row[3]).toBe(true);
    });
  });

test.describe("the Connection field across environments", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("switching environment while the list loads shows the new environment's connections only", async ({
    page,
  }) => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    const { details, recorder } = await openConnected(
      page,
      {
        bindable: {
          [PRODUCTION]: [bindable(1, { name: "prod-crm" })],
          [STAGING]: [bindable(2, { name: "staging-crm" })],
        },
        holdFirst: { [PRODUCTION]: held },
      },
      crmWorkflow(),
    );
    await details.open("crm-lookup");
    await expect.poll(() => recorder.bindable.length).toBe(1);
    await details.close();
    await switchTo(page, "Acme / Payments / Staging");
    release();
    await details.open("crm-lookup");
    await connectionSelect(details).click();
    await expect(page.locator(".choice-group")).toHaveText(["In Staging"]);
    await expect(options(page).filter({ hasText: /^staging-crm/ })).toHaveCount(
      1,
    );
    await expect(options(page).filter({ hasText: /^prod-crm/ })).toHaveCount(0);
  });
});

test.describe("the test policy when the platform can't answer", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  for (const failure of ["500", "network"] as const)
    test(`a failing policy read (${failure}) is asked again only after a pause`, async ({
      page,
    }) => {
      const { details } = await openConnected(
        page,
        { bindable: { [PRODUCTION]: [bindable(1)] } },
        crmWorkflow(
          {},
          { with: { object: { customerId: { literal: "c-1042" } } } },
        ),
      );
      let reads = 0;
      await page.route("**/environments/*/test-policy", async (route) => {
        reads++;
        if (failure === "500")
          await route.fulfill({
            status: 500,
            json: { code: "WV-INTERNAL", message: "Internal error" },
          });
        else await route.abort("failed");
      });
      await details.open("crm-lookup");
      await expect.poll(() => reads).toBeGreaterThan(0);
      // Each key renders step details again, and every render looks at the
      // policy: none of them may ask again before the pause is over.
      const customer = details.dialog.getByLabel("Customer ID", {
        exact: true,
      });
      await customer.click();
      await customer.press("End");
      await customer.pressSequentially("-17");
      await expect(customer).toHaveValue("c-1042-17");
      expect(reads).toBeLessThanOrEqual(2);
    });
});
