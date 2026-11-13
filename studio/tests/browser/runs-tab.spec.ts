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
// The Runs tab's list: a workflow's runs newest first, their filters, the
// refresh every 5 seconds, older pages, every state the list can be in, and
// its keys. The platform is mocked with the run views fake's rules.
import { expect, test } from "@playwright/test";
import { useNewEditor } from "./canvas-po";
import { DesignerPage } from "./designer-po";
import {
  listedKinds,
  olderRuns,
  openRunsTab,
  RUN_IDS,
  runFixture,
  runsPage,
  runsWorkflow,
} from "./runs-po";
import { sizes } from "./step-details-po";
import { allCapabilities, connected, newWorkflow, offline } from "./support";

for (const size of sizes)
  test.describe(`the Runs list at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    const wide = size.width >= 768;

    test("the Runs tab lists the workflow's runs newest first, with test runs", async ({
      page,
    }) => {
      const { runs, platform } = await openRunsTab(page);
      await expect(runs.rows).toHaveCount(listedKinds.length + 1);
      const ids = await runs.rows.evaluateAll((rows) =>
        rows.map((row) => row.getAttribute("data-run")),
      );
      expect(ids).toEqual([
        ...listedKinds.map((kind) => runFixture(kind).summary.id),
        RUN_IDS.unavailable,
      ]);
      const query = platform.listQueries()[0];
      expect(Object.fromEntries(query)).toEqual({
        workflow: "invoice-approval",
        include_test: "true",
        order: "started_desc",
        limit: "50",
      });
      await expect(runs.row(RUN_IDS.test)).toContainText("Test");
      await expect(runs.row(RUN_IDS.unavailable)).toContainText(
        "UnavailableStudio can't show this run.",
      );
      await expect(runs.list).toHaveAttribute(
        "aria-label",
        "Runs of invoice-approval",
      );
      await expect(runs.panel.locator(".runs-list-head")).toContainText(
        `in ${"development"}`,
      );
    });

    test("a row says how the run went, when, how long, which version and how it started", async ({
      page,
    }) => {
      const { runs } = await openRunsTab(page);
      const failed = runs.row(RUN_IDS.failed);
      await expect(failed.locator(".run-status")).toHaveText("Failed");
      await expect(failed.locator(".run-at")).toHaveText("at send");
      await expect(failed.locator(".run-when")).toHaveText("12 min ago");
      await expect(failed.locator(".run-version")).toHaveText("1.3.0");
      await expect(failed.getByRole("img", { name: "Webhook" })).toHaveCount(1);
      await expect(runs.row(RUN_IDS.retry)).toContainText("Retry of 1c0d7f8a");
      await expect(runs.row(RUN_IDS.older)).toContainText(
        "Called by run 9b1c4e2a",
      );
      // A version other than the draft's stands out.
      await expect(
        runs.row(RUN_IDS.older).locator(".run-version"),
      ).toHaveAttribute("data-older", "");
      await expect(
        runs.row(RUN_IDS.failed).locator(".run-version"),
      ).not.toHaveAttribute("data-older", "");
      // A run still going counts its duration up.
      const duration = runs.row(RUN_IDS.running).locator(".run-duration");
      const before = await duration.textContent();
      await page.clock.runFor(2_000);
      await expect(duration).not.toHaveText(before ?? "");
    });

    test("filters by status, version and how it started, and Clear filters resets them", async ({
      page,
    }) => {
      const { runs, platform } = await openRunsTab(page);
      await expect(runs.rows).toHaveCount(listedKinds.length + 1);
      const address = page.url();
      const last = () => Object.fromEntries(platform.listQueries().at(-1)!);

      await runs.filters.click();
      const popover = page.getByRole("dialog", { name: "Filters" });
      await expect(popover).toBeVisible();
      await popover.getByRole("checkbox", { name: "Failed" }).check();
      await expect.poll(last).toMatchObject({ status: "failed" });
      await popover.getByLabel("Version").selectOption("1.2.0");
      await expect.poll(last).toMatchObject({ version: "1.2.0" });
      await popover.getByLabel("How it started").selectOption("Webhook");
      await expect.poll(last).toMatchObject({ origin: "webhook" });
      await popover.getByRole("button", { name: "Done" }).click();
      await expect(popover).toBeHidden();
      await expect(runs.filters).toBeFocused();
      await expect(runs.filters).toContainText("3");
      // No run is a failed 1.2.0 webhook run.
      await expect(runs.panel).toContainText("No runs match these filters.");
      await expect(
        runs.panel.getByRole("button", { name: /^Remove/ }),
      ).toHaveText(["", "", ""]);
      // Filters stay in memory: never in the address.
      expect(page.url()).toBe(address);

      await runs.panel
        .getByRole("button", { name: "Remove Version: 1.2.0" })
        .click();
      await expect.poll(last).not.toHaveProperty("version");
      await runs.panel
        .getByRole("switch", { name: "Include test runs" })
        .click();
      await expect.poll(last).toMatchObject({ include_test: "false" });

      await runs.panel
        .getByRole("button", { name: "Clear filters" })
        .first()
        .click();
      await expect.poll(last).toEqual({
        workflow: "invoice-approval",
        include_test: "true",
        order: "started_desc",
        limit: "50",
      });
      await expect(runs.rows).toHaveCount(listedKinds.length + 1);
      expect(page.url()).toBe(address);
    });

    test("the list refreshes every 5 seconds while visible", async ({
      page,
    }) => {
      const { runs, platform } = await openRunsTab(page);
      await expect(runs.rows).toHaveCount(listedKinds.length + 1);
      const asked = platform.listQueries().length;
      const newer = runFixture("succeeded");
      newer.summary = {
        ...newer.summary,
        id: "7a6b5c4d-3e2f-4a1b-8c9d-0e1f2a3b4c5d",
        started_at: new Date(Date.now()).toISOString(),
      };
      platform.runs.unshift(newer);
      await page.clock.runFor(5_000);
      await expect
        .poll(() => platform.listQueries().length)
        .toBeGreaterThan(asked);
      await expect(runs.rows.first()).toHaveAttribute(
        "data-run",
        newer.summary.id,
      );
    });

    test("Show older runs loads the next page", async ({ page }) => {
      const { runs, platform } = await openRunsTab(page, {
        runs: [...listedKinds.map(runFixture), ...olderRuns(50)],
        unavailable: false,
      });
      await expect(runs.rows).toHaveCount(50);
      await runs.panel.getByRole("button", { name: "Show older runs" }).click();
      await expect(runs.rows).toHaveCount(listedKinds.length + 50);
      expect(platform.listQueries().at(-1)?.get("cursor")).toBeTruthy();
      await expect(
        runs.panel.getByRole("button", { name: "Show older runs" }),
      ).toHaveCount(0);
    });

    test("arrow keys move between runs and Enter shows one", async ({
      page,
    }) => {
      const { runs, platform } = await openRunsTab(page);
      await expect(runs.rows).toHaveCount(listedKinds.length + 1);
      // The newest run is shown once the list loads, except on a narrow
      // screen, where the list stays in front.
      await expect(runs.rows.first()).toHaveAttribute(
        "aria-selected",
        wide ? "true" : "false",
      );
      const reads = () =>
        platform.requests.filter((url) => /\/runs\/[^/]+$/.test(url.pathname))
          .length;
      await expect.poll(reads).toBe(wide ? 1 : 0);
      await runs.rows.first().focus();
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("ArrowDown");
      const third = runs.rows.nth(2);
      await expect(third).toBeFocused();
      await expect(third).toHaveAttribute("aria-selected", "true");
      await expect(third).toHaveAttribute("tabindex", "0");
      await expect(runs.rows.first()).toHaveAttribute("tabindex", "-1");
      await page.keyboard.press("Enter");
      await expect
        .poll(() =>
          platform.requests.some((url) =>
            url.pathname.endsWith(`/runs/${RUN_IDS.test}`),
          ),
        )
        .toBe(true);
      if (wide) await expect(third).toBeFocused();
      else {
        // The run comes to the front: focus goes with it, not to the hidden list.
        const back = page.getByRole("button", { name: "Back to runs" });
        await expect(back).toBeFocused();
        await page.keyboard.press("End");
        // The hidden list took no key: the run shown stays the chosen one.
        await expect(
          runs.panel.locator(`[data-run="${RUN_IDS.test}"]`),
        ).toHaveAttribute("aria-selected", "true");
        await back.click();
        await expect(third).toBeFocused();
      }
      await page.keyboard.press("End");
      await expect(runs.rows.last()).toHaveAttribute("aria-selected", "true");
      await page.keyboard.press("Home");
      await expect(runs.rows.first()).toBeFocused();
      await page.keyboard.press("?");
      const sheet = page.getByRole("dialog", { name: "Keyboard shortcuts" });
      await expect(sheet.getByRole("heading", { name: "Runs" })).toBeVisible();
      await expect(sheet).toContainText("Show the run on the canvas");
    });
  });

test.describe("the Runs list's states", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("without a platform, the Runs tab says runs live on one", async ({
    page,
  }) => {
    await useNewEditor(page);
    await offline(page);
    await newWorkflow(page);
    await new DesignerPage(page).setSource(JSON.stringify(runsWorkflow));
    await page.getByRole("tab", { name: "Runs" }).click();
    const runs = runsPage(page);
    await expect(runs.panel).toContainText("Runs live on a platform.");
    await expect(runs.panel).toContainText(
      "Connect to see this workflow's runs.",
    );
    await expect(
      runs.panel.getByRole("button", { name: "Connect" }),
    ).toBeVisible();
    await expect(runs.panel.locator(".runs-list-head")).toContainText(
      "on this computer",
    );
  });

  test("without an environment, the Runs tab asks for one", async ({
    page,
  }) => {
    await useNewEditor(page);
    await connected(page, { environmentId: "" });
    await newWorkflow(page);
    await new DesignerPage(page).setSource(JSON.stringify(runsWorkflow));
    await page.getByRole("tab", { name: "Runs" }).click();
    const runs = runsPage(page);
    await expect(runs.panel).toContainText(
      "Choose an environment to see its runs.",
    );
    await expect(
      runs.panel.getByRole("button", { name: "Choose environment" }),
    ).toBeVisible();
  });

  test("a refused list shows who can grant access and no run", async ({
    page,
  }) => {
    const { runs, platform } = await openRunsTab(page);
    await expect(runs.rows).toHaveCount(listedKinds.length + 1);
    platform.listFailure = {
      status: 403,
      code: "WV-FORBIDDEN",
      message: "Not allowed.",
    };
    await runs.panel.getByRole("button", { name: "Refresh runs" }).click();
    await expect(runs.panel).toContainText("You need run.read in development.");
    await expect(runs.panel).toContainText(
      "Ask an administrator in Settings › People and access.",
    );
    await expect(runs.rows).toHaveCount(0);
    // Nothing read before stays, on the canvas either.
    await expect(runs.canvas.locator(".run-header")).toHaveCount(0);
  });

  test("without run.read, the Runs tab asks nothing and says who can grant it", async ({
    page,
  }) => {
    const { runs, platform } = await openRunsTab(page, {
      capabilities: allCapabilities.filter((c) => c !== "run.read"),
    });
    await expect(runs.panel).toContainText("You need run.read in development.");
    expect(platform.listQueries()).toHaveLength(0);
  });

  test("an older platform says it can't list runs", async ({ page }) => {
    const { runs, platform } = await openRunsTab(page, {
      before: (p) => {
        p.listFailure = {
          status: 501,
          code: "WV-UNAVAILABLE",
          message: "Not served.",
        };
      },
    });
    await expect(runs.panel).toContainText(
      "This platform can't list runs yet.",
    );
    await expect(runs.panel).toContainText(
      "Runs show here once the platform is updated.",
    );
    const asked = platform.listQueries().length;
    await page.clock.runFor(30_000);
    expect(platform.listQueries()).toHaveLength(asked);
  });

  test("the list shows skeleton rows while it loads, then the runs", async ({
    page,
  }) => {
    let release = () => {};
    const { runs } = await openRunsTab(page, {
      before: (p) => {
        release = p.holdList();
      },
    });
    await expect(runs.panel.locator(".run-skeleton")).toHaveCount(7);
    await expect(runs.panel.getByRole("status")).toHaveText("Loading runs…");
    release();
    await expect(runs.rows).toHaveCount(listedKinds.length + 1);
    await expect(runs.panel.locator(".run-skeleton")).toHaveCount(0);
  });

  test("a failed refresh keeps the rows, says why and offers Try again", async ({
    page,
  }) => {
    const { runs, platform } = await openRunsTab(page);
    await expect(runs.rows).toHaveCount(listedKinds.length + 1);
    platform.listFailure = {
      status: 503,
      code: "WV-UNAVAILABLE",
      message: "The platform didn't answer in time.",
    };
    await runs.panel.getByRole("button", { name: "Refresh runs" }).click();
    const notice = runs.panel.locator(".runs-error");
    await expect(notice).toContainText("Studio couldn't refresh the runs.");
    await expect(notice).toContainText(
      "WV-UNAVAILABLE: The platform didn't answer in time.",
    );
    await expect(runs.rows).toHaveCount(listedKinds.length + 1);
    platform.listFailure = null;
    await notice.getByRole("button", { name: "Try again" }).click();
    await expect(notice).toHaveCount(0);
  });

  test("an empty list says when runs appear", async ({ page }) => {
    const { runs } = await openRunsTab(page, { runs: [], unavailable: false });
    await expect(runs.panel).toContainText("No runs of invoice-approval yet.");
    await expect(runs.panel).toContainText(
      "Runs appear here once it's activated and started.",
    );
    await expect(
      runs.panel.getByRole("button", { name: "Clear filters" }),
    ).toHaveCount(0);
  });
});
