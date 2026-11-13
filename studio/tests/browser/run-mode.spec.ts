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
// Step details in run mode: a step of the run chosen in the Runs tab, read
// only, with what it returned in that run (or why the platform left it out),
// what it received, recomputed from the run's data, its iterations, the run
// it called, its logs, and nothing of the run kept once it's left.
import { expect, test, type Page } from "@playwright/test";
import type { RunLogEntry } from "../../src/app/operate/run-contracts";
import {
  CHILD_RUN,
  listedKinds,
  openRunsTab,
  RUN_IDS,
  runFixture,
  type RunFixture,
  type RunsPage,
  type RunsPlatform,
} from "./runs-po";
import { sizes, StepDetailsPage } from "./step-details-po";
import { tokenColor } from "./support";

/** The Runs tab with `id` chosen, its run drawn. */
async function choose(page: Page, runs: RunsPage, id: string) {
  await expect(runs.rows.first()).toBeVisible();
  await runs.row(id).click();
  // This run's header, its facts read: its data has loaded.
  const header = runs.canvas.locator(".run-header");
  await expect(header.getByRole("heading")).toHaveText(`Run ${id.slice(0, 8)}`);
  await expect(header.locator(".run-caption")).toBeVisible();
  return new StepDetailsPage(page);
}
const outputRegion = (details: StepDetailsPage) =>
  details.dialog.locator('[data-region="output"]');
const inputRegion = (details: StepDetailsPage) =>
  details.dialog.locator('[data-region="input"]');
/** Shows a pane on a narrow screen, where step details shows one at a time. */
async function showPane(
  details: StepDetailsPage,
  name: "Input" | "Parameters" | "Output",
) {
  const tab = details.dialog
    .getByRole("tablist", { name: "Step details panes" })
    .getByRole("tab", { name });
  if (await tab.isVisible()) await tab.click();
}
const outputReads = (platform: RunsPlatform) =>
  platform.requests.filter(
    (url) =>
      url.pathname.endsWith("/steps") &&
      url.searchParams.get("include") === "output",
  );

for (const size of sizes)
  test.describe(`step details in run mode at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("every field is read-only and Execute step is absent", async ({
      page,
    }) => {
      const { runs } = await openRunsTab(page);
      const details = await choose(page, runs, RUN_IDS.failed);
      await details.openWithKeyboard("send");
      // Step details opens on what the step returned: the Output pane itself.
      await expect(outputRegion(details)).toBeFocused();
      // The neighbor steps' buttons stay off the panes' edges: the top bar's
      // Previous and Next move between steps.
      await expect(details.dialog.locator(".sd-neighbors:visible")).toHaveCount(
        0,
      );
      await showPane(details, "Parameters");
      const note = details.dialog.locator(".sd-run-note");
      await expect(note).toContainText(
        "You're looking at run 1c0d7f8a. Go back to editing to change this step.",
      );
      await expect(
        details.dialog.getByRole("button", { name: "Execute step" }),
      ).toHaveCount(0);
      const editable = async () =>
        details.dialog
          .locator(".sd-tabpanel:not([hidden])")
          .evaluate((panel) =>
            [
              ...panel.querySelectorAll<HTMLElement>(
                "input, textarea, select, [contenteditable='true']",
              ),
            ]
              .filter(
                (field) =>
                  !(field as HTMLInputElement).readOnly &&
                  !(field as HTMLInputElement).disabled &&
                  field.getAttribute("aria-readonly") !== "true" &&
                  field.getAttribute("aria-disabled") !== "true",
              )
              .map((field) => field.outerHTML.slice(0, 80)),
          );
      expect(await editable()).toEqual([]);
      // Each field reads as its value: no mode switch, menu or chevron, and
      // no field box drawn around it.
      const panel = details.dialog.locator(".sd-tabpanel:not([hidden])");
      await expect(panel.locator(".param-mode:visible")).toHaveCount(0);
      await expect(panel.locator(".param-menu:visible")).toHaveCount(0);
      await expect(panel.locator(".choice-field button:visible")).toHaveCount(
        0,
      );
      await expect(
        panel.locator('.param-resource-modes [aria-checked="false"]:visible'),
      ).toHaveCount(0);
      const sunken = await tokenColor(page, "--sunken");
      for (const field of await panel
        .locator(".choice-field, button.ref-pill-button")
        .all()) {
        await expect(field).toHaveCSS("background-color", sunken);
        await expect(field).toHaveCSS("border-top-color", "rgba(0, 0, 0, 0)");
      }
      await details.tab("Settings").click();
      expect(await editable()).toEqual([]);
      // Rename and Pin do nothing.
      await expect(
        details.dialog.getByRole("button", { name: "Rename send" }),
      ).toHaveCount(0);
      await details.dialog.locator(".sd-head").focus();
      await page.keyboard.press("p");
      await expect(
        details.dialog.getByRole("button", { name: /^(Un)?pin output$/ }),
      ).toHaveCount(0);
    });

    test("Output shows the step's recorded output, read when the step opens", async ({
      page,
    }) => {
      const { runs, platform } = await openRunsTab(page);
      const details = await choose(page, runs, RUN_IDS.failed);
      await details.openWithKeyboard("fetch");
      await showPane(details, "Output");
      const output = outputRegion(details);
      await expect(output.locator(".pane-source-tag")).toHaveText(
        "From run 1c0d7f8a",
      );
      await output.getByRole("radio", { name: "JSON" }).click();
      await expect(output.locator(".pane-json")).toContainText('"Ada"');
      const reads = outputReads(platform);
      expect(reads).toHaveLength(1);
      expect(Object.fromEntries(reads[0].searchParams)).toEqual({
        step: "fetch",
        include: "output",
        limit: "200",
      });
    });

    test("an output the platform left out says why", async ({ page }) => {
      const { runs, platform } = await openRunsTab(page);
      const run = platform.runs.find(
        (item) => item.summary.id === RUN_IDS.failed,
      )!;
      run.facts = run.facts.map((fact) =>
        fact.node_id === "fetch"
          ? {
              ...fact,
              output: undefined,
              omissions: [{ path: "/output", reason: "classified_secret" }],
            }
          : fact,
      );
      for (const fact of run.facts)
        if (fact.output === undefined) delete fact.output;
      const details = await choose(page, runs, RUN_IDS.failed);
      await details.openWithKeyboard("fetch");
      await showPane(details, "Output");
      const output = outputRegion(details);
      await expect(output).toContainText(
        "This output is classified as secret.",
      );
      await expect(output.locator(".pane-sub")).toContainText("Not shown");
      await expect(output).not.toContainText("Ada");
    });

    test("Input shows what the step received, recomputed from the run's data", async ({
      page,
    }) => {
      const { runs } = await openRunsTab(page);
      const details = await choose(page, runs, RUN_IDS.failed);
      await details.openWithKeyboard("send");
      await showPane(details, "Input");
      const input = inputRegion(details);
      await expect(input.getByLabel("Input source")).toHaveValue("received");
      await expect(input.locator(".pane-source-tag")).toHaveText(
        "Recomputed from the run's data",
      );
      await expect(input.locator(".pane-received")).toHaveText(
        JSON.stringify({ customer: "Ada", total: 120.5 }, null, 2),
      );
      // What it received has no fields to list: JSON shows, Table works,
      // Schema can't be chosen.
      const views = input.getByRole("radiogroup", { name: "Input view" });
      await expect(views.getByRole("radio", { name: "JSON" })).toHaveAttribute(
        "aria-checked",
        "true",
      );
      await expect(
        views.getByRole("radio", { name: "Schema" }),
      ).toHaveAttribute("aria-disabled", "true");
      await views.getByRole("radio", { name: "Table" }).click();
      await expect(input.getByRole("table")).toContainText("Ada");
    });

    test("the failed step shows the run's error", async ({ page }) => {
      const { runs } = await openRunsTab(page);
      const details = await choose(page, runs, RUN_IDS.failed);
      await details.openWithKeyboard("send");
      await showPane(details, "Output");
      await expect(outputRegion(details)).toContainText(
        "This step failed in run 1c0d7f8a: SMTP_REJECTED.",
      );
      await expect(outputRegion(details).locator(".pane-sub")).toContainText(
        "Failed in this run",
      );
      // A failed step returns nothing to count.
      await expect(outputRegion(details).locator(".pane-count")).toHaveText("");
      // The callout reads in text color; danger is for its icon and border.
      await expect(
        outputRegion(details)
          .locator(".pane-run-notice[data-tone=danger] p")
          .first(),
      ).toHaveCSS("color", await tokenColor(page, "--text"));
    });

    test("step logs say when the platform doesn't serve them, and ask once", async ({
      page,
    }) => {
      const { runs, platform } = await openRunsTab(page);
      const details = await choose(page, runs, RUN_IDS.failed);
      const logReads = () =>
        platform.requests.filter((url) => url.pathname.endsWith("/logs"));
      for (const id of ["fetch", "send"]) {
        await details.openWithKeyboard(id);
        await showPane(details, "Output");
        await outputRegion(details)
          .getByRole("button", { name: "Step logs" })
          .click();
        await expect(outputRegion(details)).toContainText(
          "Logs aren't available from this platform yet.",
        );
        await details.close();
      }
      expect(logReads()).toHaveLength(1);
      expect(logReads()[0].searchParams.get("node_id")).toBe("fetch");
      expect(logReads()[0].searchParams.get("limit")).toBe("50");
    });
  });

test.describe("step details in run mode", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the draft's pin stays out of run mode, and nothing there changes test data", async ({
    page,
  }) => {
    const { runs } = await openRunsTab(page);
    await expect(runs.rows.first()).toBeVisible();
    // Pin a sample of fetch in the draft first.
    await runs.tabs.getByRole("tab", { name: "Designer" }).click();
    // The editable canvas, not the Runs tab's, is on screen.
    await expect(page.locator(".canvas-v2")).not.toHaveClass(/viewing-runs/);
    await expect(page.locator(".canvas-v2")).toHaveAttribute("data-ready", "");
    const designer = new StepDetailsPage(page);
    await designer.open("fetch");
    const pane = designer.dialog.locator('[data-region="output"]');
    await pane.getByRole("button", { name: "Generate sample" }).click();
    await expect(pane.locator(".pinned-banner, .pill")).toContainText([
      "Pinned",
    ]);
    await designer.close();
    const saves: string[] = [];
    page.on("request", (request) => {
      if (request.method() !== "GET" && /test-?data/i.test(request.url()))
        saves.push(`${request.method()} ${request.url()}`);
    });
    await runs.tabs.getByRole("tab", { name: "Runs" }).click();
    const details = await choose(page, runs, RUN_IDS.failed);
    await details.openWithKeyboard("fetch");
    const output = outputRegion(details);
    await expect(output.locator(".pane-source-tag")).toHaveText(
      "From run 1c0d7f8a",
    );
    await expect(output.locator(".pinned-banner")).toHaveCount(0);
    await expect(
      output.getByRole("button", { name: /^(Unpin|Keep pinned)/ }),
    ).toHaveCount(0);
    await expect(output).not.toContainText(/pinned/i);
    await details.dialog.locator(".sd-head").focus();
    await page.keyboard.press("p");
    await details.close();
    expect(saves).toEqual([]);
  });

  test("while a chosen run loads, step details say which run they're loading", async ({
    page,
  }) => {
    const { runs, platform } = await openRunsTab(page);
    await expect(runs.rows).toHaveCount(listedKinds.length + 1);
    // The newest run opened with the tab; wait for it, then hold the next read.
    await expect(runs.canvas.locator(".run-header .run-caption")).toBeVisible();
    const release = platform.holdRead();
    await runs.row(RUN_IDS.failed).click();
    const details = new StepDetailsPage(page);
    await details.openWithKeyboard("fetch");
    const output = outputRegion(details);
    await expect(output.getByRole("status")).toHaveText(
      "Loading run 1c0d7f8a…",
    );
    await expect(details.dialog.locator(".sd-run-note")).toContainText(
      "You're looking at run 1c0d7f8a. Go back to editing to change this step.",
    );
    release();
    await expect(output.locator(".pane-source-tag")).toHaveText(
      "From run 1c0d7f8a",
    );
  });

  test("a refused read of step logs forgets the run", async ({ page }) => {
    const { runs, platform } = await openRunsTab(page);
    const details = await choose(page, runs, RUN_IDS.failed);
    await details.openWithKeyboard("fetch");
    const output = outputRegion(details);
    await output.getByRole("radio", { name: "JSON" }).click();
    await expect(output.locator(".pane-json")).toContainText('"Ada"');
    await page.route("**/runs/*/logs**", (route) =>
      route.fulfill({
        status: 403,
        contentType: "application/problem+json",
        body: JSON.stringify({ code: "WV-FORBIDDEN", message: "Not allowed." }),
      }),
    );
    await output.getByRole("button", { name: "Step logs" }).click();
    await expect(runs.panel).toContainText("You need run.read in development.");
    await expect(runs.canvas.locator(".run-header")).toHaveCount(0);
    await expect(details.dialog).not.toContainText("Ada");
  });

  test("a step that ran several times offers its iterations, the failed one first", async ({
    page,
  }) => {
    const { runs } = await openRunsTab(page, {
      runs: [...listedKinds.map(runFixture), runFixture("loop")],
    });
    const details = await choose(page, runs, RUN_IDS.loop);
    await details.openWithKeyboard("send");
    const iteration = details.dialog.getByLabel("Iteration");
    await expect(iteration.locator("option:checked")).toHaveText(
      "Item 2 of 3 · Failed",
    );
    await expect(outputRegion(details)).toContainText(
      "This step failed in run 8e7f5b0c: SMTP_REJECTED.",
    );
    await iteration.selectOption({ label: "Item 1 of 3 · Finished" });
    const output = outputRegion(details);
    await output.getByRole("radio", { name: "JSON" }).click();
    await expect(output.locator(".pane-json")).toContainText('"m-0"');
    await expect(output.locator(".pane-source-tag")).toHaveText(
      "From run 8e7f5b0c, item 1",
    );
  });

  test("a call step names the run it called", async ({ page }) => {
    const { runs } = await openRunsTab(page, {
      runs: [...listedKinds.map(runFixture), runFixture("call")],
    });
    const details = await choose(page, runs, RUN_IDS.call);
    await details.openWithKeyboard("send");
    const output = outputRegion(details);
    await expect(output).toContainText("Called run 9b1c4e2a");
    await output.getByRole("button", { name: "Open in Operate" }).click();
    await expect(page).toHaveURL(new RegExp(`/runs/${CHILD_RUN}$`));
  });

  test("step logs show the platform's entries for the step", async ({
    page,
  }) => {
    const entry: RunLogEntry = {
      id: "l1",
      at: "2026-10-07T11:48:02Z",
      source: "connector",
      level: "error",
      code: "SMTP_REJECTED",
      message: "The mail server refused the message.",
      node_id: "send",
      instance_key: "send",
      attempt: 1,
      worker_id: null,
      fields: {},
      redactions: 1,
      truncated: false,
    };
    const { runs } = await openRunsTab(page, { logs: [entry] });
    const details = await choose(page, runs, RUN_IDS.failed);
    await details.openWithKeyboard("send");
    const output = outputRegion(details);
    await output.getByRole("button", { name: "Step logs" }).click();
    const row = output.locator(".run-log-row");
    await expect(row).toHaveCount(1);
    await expect(row).toContainText("The mail server refused the message.");
    await expect(row).toContainText("SMTP_REJECTED");
    await expect(row).toContainText("1 value hidden");
  });

  test("a large output says it's too large", async ({ page }) => {
    const { runs, platform } = await openRunsTab(page);
    platform.outputFailure = {
      status: 429,
      code: "WV-PAGE-LIMIT",
      message: "The page is larger than 8 MiB.",
    };
    const details = await choose(page, runs, RUN_IDS.failed);
    await details.openWithKeyboard("fetch");
    await expect(outputRegion(details)).toContainText(
      "This output is too large to show.",
    );
    // Fewer instances a page each time, down to one.
    expect(
      outputReads(platform).map((url) => Number(url.searchParams.get("limit"))),
    ).toEqual([200, 100, 50, 25, 12, 6, 3, 1]);
  });

  test("a run's data isn't kept anywhere", async ({ page }) => {
    const marker = "MARKER-7f3c9e";
    const messages: string[] = [];
    page.on("console", (message) => messages.push(message.text()));
    // Whatever Studio sends anywhere (test data saves included).
    const sent: string[] = [];
    page.on("request", (request) => {
      if (request.method() !== "GET")
        sent.push(`${request.url()} ${request.postData() ?? ""}`);
    });
    const marked = (run: RunFixture): RunFixture => ({
      ...run,
      facts: run.facts.map((fact) =>
        fact.node_id === "fetch"
          ? { ...fact, output: { rows: [{ name: marker }] } }
          : fact,
      ),
    });
    const { runs } = await openRunsTab(page, {
      runs: listedKinds.map(runFixture).map(marked),
    });
    const details = await choose(page, runs, RUN_IDS.failed);
    await details.openWithKeyboard("fetch");
    const output = outputRegion(details);
    await output.getByRole("radio", { name: "JSON" }).click();
    await expect(output.locator(".pane-json")).toContainText(marker);
    await details.close();
    await runs.canvas.getByRole("button", { name: "Back to editing" }).click();
    await expect(
      runs.tabs.getByRole("tab", { name: "Designer" }),
    ).toHaveAttribute("aria-selected", "true");
    const kept = await page.evaluate(async () => {
      const found: string[] = [location.href];
      for (const store of [localStorage, sessionStorage])
        for (let i = 0; i < store.length; i++) {
          const key = store.key(i)!;
          found.push(key, store.getItem(key) ?? "");
        }
      for (const info of await indexedDB.databases()) {
        if (!info.name) continue;
        const db = await new Promise<IDBDatabase>((resolve, reject) => {
          const open = indexedDB.open(info.name!);
          open.onsuccess = () => resolve(open.result);
          open.onerror = () => reject(open.error);
        });
        for (const name of db.objectStoreNames) {
          const all = await new Promise<unknown[]>((resolve) => {
            const read = db
              .transaction(name, "readonly")
              .objectStore(name)
              .getAll();
            read.onsuccess = () => resolve(read.result);
            read.onerror = () => resolve([]);
          });
          found.push(JSON.stringify(all));
        }
        db.close();
      }
      return found.join("\n");
    });
    expect(kept).not.toContain(marker);
    expect(messages.join("\n")).not.toContain(marker);
    expect(sent.filter((request) => request.includes(marker))).toEqual([]);
  });

  test("? shows the shortcuts in run-mode step details", async ({ page }) => {
    const { runs } = await openRunsTab(page);
    const details = await choose(page, runs, RUN_IDS.failed);
    await details.openWithKeyboard("fetch");
    await page.keyboard.press("?");
    await expect(
      page.getByRole("dialog", { name: "Keyboard shortcuts" }),
    ).toBeVisible();
  });
});

test.describe("step details in run mode on a phone", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("the run's chip stands in for the breadcrumb", async ({ page }) => {
    const { runs } = await openRunsTab(page);
    const details = await choose(page, runs, RUN_IDS.failed);
    await details.openWithKeyboard("send");
    await expect(details.dialog.locator(".sd-run-chip")).toBeVisible();
    await expect(details.dialog.locator(".sd-crumb")).toHaveCount(0);
  });
});
