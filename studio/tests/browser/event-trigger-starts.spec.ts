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
// The Platform event trigger's Starts tab: what it started in the environment
// Studio is connected to, with the run each start started or why it started
// none, and first the stop an activation still owes, made here with the same
// checks as there.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { refusal, requested, startedRuns } from "../event-platform";
import {
  openConnected,
  openStarts,
  showStarts,
  startsTab,
  storedIntent,
  WIDER,
} from "./event-activation-po";
import { eventTile, notifyTeam, openEventTrigger } from "./event-trigger-po";
import { routePlatformEvent } from "./local-authoring";
import {
  openStepFixture,
  StepDetailsPage,
  withLanguageFeatures,
} from "./step-details-po";
import { offline } from "./support";

const CURRENT = {
  id: "t-2",
  name: "notify-team-2",
  activation: "a-old",
  revision: 1,
};
const OLD = {
  id: "t-1",
  name: "notify-team",
  activation: "a-older",
  revision: 3,
};
const OWED = { triggerId: "t-1", revision: 3, activationId: "a-older" };
const RUN = "9b1c4e2a-1d2e-4f3a-8b4c-5d6e7f8a9b0c";
const SOURCE = "3fa2c1d0-6b5e-4d7c-9a8f-2e1d0c9b8a7f";
const box = async (locator: Locator) => (await locator.boundingBox())!;
const rows = (panel: Locator) =>
  panel.getByRole("list", { name: "Starts" }).getByRole("listitem");
const start = (fields: Record<string, unknown>) => ({
  ...requested(String(fields["id"])),
  source_run_id: SOURCE,
  ...fields,
});
/** One start of each kind, newest first. */
const EACH = [
  start({ id: "s-1", status: "started", run_id: RUN }),
  start({ id: "s-2", event_type: "transaction.compensated" }),
  start({ id: "s-3", status: "skipped", code: "WV-TRIGGER-EVENT-DEPTH" }),
  start({
    id: "s-4",
    status: "rejected",
    code: "WV-TRIGGER-START",
    cause: "WV-FORBIDDEN",
  }),
  start({ id: "s-5", status: "rejected", code: "WV-TRIGGER-START-EXPIRED" }),
];
const open = (page: Page, options: Parameters<typeof openStarts>[1] = {}) =>
  openStarts(page, { seeds: [CURRENT], stored: "t-2", ...options });

test("lists what the trigger started, newest first, with why a start started nothing", async ({
  page,
}) => {
  const opened = await openConnected(page, { seeds: [CURRENT], stored: "t-2" });
  opened.platform.starts.set("t-2", EACH);
  const { panel } = await showStarts(page);
  await expect(panel).toContainText("In development");
  await expect(panel).toContainText(
    /Trigger notify-team-2\s*·\s*reacts for 1\.1\.0/,
  );
  await expect(panel).toContainText("Updated just now");
  await expect(rows(panel)).toHaveText([
    /Started run 9b1c4e2a\s*Needs attention in run 3fa2c1d0 of move-between-systems/,
    /Waiting to start\s*Undone in run 3fa2c1d0 of move-between-systems/,
    /Skipped\s*Platform events from a run this deep in a chain of reactions start no workflows\./,
    /Couldn't start\s*The triggered workflow couldn't start\. Its owner can no longer start or read runs here\./,
    /Couldn't start\s*The workflow couldn't start within a day of the event, so the event no longer starts it\./,
  ]);
  await expect(panel).toContainText(
    "Runs it started are also in the Runs tab, under Platform event.",
  );
  await expect(
    panel.getByRole("button", { name: "Open run 9b1c4e2a" }),
  ).toHaveCount(1);
  await expect(panel.getByRole("button", { name: /^Open run/ })).toHaveCount(1);
  await expect(panel.getByRole("button", { name: "Show more" })).toHaveCount(0);
});

test("shows twenty at a time; Show more moves focus to the first new one", async ({
  page,
}) => {
  const opened = await openConnected(page, { seeds: [CURRENT], stored: "t-2" });
  opened.platform.starts.set("t-2", startedRuns(25));
  const { panel } = await showStarts(page);
  await expect(rows(panel)).toHaveCount(20);
  await panel.getByRole("button", { name: "Show more" }).click();
  await expect(rows(panel)).toHaveCount(25);
  await expect(rows(panel).nth(20)).toBeFocused();
  await expect(panel.getByRole("button", { name: "Show more" })).toHaveCount(0);
});

test("Open run opens the Runs tab on that run", async ({ page }) => {
  const opened = await openConnected(page, { seeds: [CURRENT], stored: "t-2" });
  opened.platform.starts.set("t-2", EACH);
  const { panel, dialog } = await showStarts(page);
  const read = page.waitForRequest((request) =>
    new URL(request.url()).pathname.endsWith(
      `/environments/development/runs/${RUN}`,
    ),
  );
  await panel.getByRole("button", { name: "Open run 9b1c4e2a" }).click();
  await read;
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("tab", { name: "Runs", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
});

test("refreshes what it shows when asked", async ({ page }) => {
  const opened = await openConnected(page, { seeds: [CURRENT], stored: "t-2" });
  const { panel } = await showStarts(page);
  await expect(panel).toContainText("Nothing started yet.");
  await expect(panel).toContainText(
    "Runs start when a matching transaction event happens.",
  );
  opened.platform.starts.set("t-2", EACH.slice(1, 2));
  await panel.getByRole("button", { name: "Refresh the starts" }).click();
  await expect(rows(panel)).toHaveText([/Waiting to start/]);
});

test("says there is no trigger here, without asking the platform", async ({
  page,
}) => {
  const { asked } = await openConnected(page, { seeds: [], stored: null });
  const { panel } = await showStarts(page);
  await expect(panel).toContainText(
    "No platform event trigger in development yet.",
  );
  await expect(panel).toContainText("Activate this workflow to create it.");
  expect(asked).toEqual([]);
});

test("a failed read is tried again after its pause, and a refusal says what it needs", async ({
  page,
}) => {
  const { platform } = await openConnected(page, {
    seeds: [CURRENT],
    stored: "t-2",
  });
  const read = "GET /event-triggers/t-2";
  platform.refuse.set(read, refusal(503, "WV-UNAVAILABLE", "Try again later."));
  const { panel } = await showStarts(page);
  await expect(panel).toContainText(
    "Studio couldn't load what this trigger started.",
  );
  const again = panel.getByRole("button", { name: /^Try again/ });
  // Kept in place while it waits, so focus isn't lost: pressing it asks nothing.
  await again.focus();
  const asked = platform.sent.length;
  await page.keyboard.press("Enter");
  await expect(again).toHaveText(/Try again in [1-5] s/);
  await expect(again).toBeDisabled();
  await expect(again).toBeFocused();
  expect(platform.sent).toHaveLength(asked);
  await expect(again).toBeEnabled({ timeout: 8_000 });
  await expect(again).toBeFocused();
  platform.refuse.set(read, refusal(403, "WV-FORBIDDEN", "Access denied"));
  await page.keyboard.press("Enter");
  await expect(panel).toContainText(
    "You can't see what this trigger started in development.",
  );
  await expect(panel).toContainText(
    "It needs permission to manage triggers and read runs there.",
  );
  // The button is gone: focus is on what took its place.
  await expect(panel.locator(".es-empty")).toBeFocused();
});

test("Try again keeps focus while it fails, then moves it to the list it read", async ({
  page,
}) => {
  const { platform } = await openConnected(page, {
    seeds: [CURRENT],
    stored: "t-2",
  });
  platform.starts.set("t-2", startedRuns(3));
  const read = "GET /event-triggers/t-2";
  platform.refuse.set(read, refusal(503, "WV-UNAVAILABLE", "Try again later."));
  const { panel } = await showStarts(page);
  const again = panel.getByRole("button", { name: /^Try again/ });
  await expect(again).toBeEnabled({ timeout: 8_000 });
  // It fails again: focus stays on the button.
  platform.refuse.set(read, refusal(503, "WV-UNAVAILABLE", "Try again later."));
  await again.focus();
  await page.keyboard.press("Enter");
  await expect(again).toHaveText(/Try again in (?:10|9|8|7) s/);
  await expect(again).toBeFocused();
  await expect(again).toBeEnabled({ timeout: 12_000 });
  await page.keyboard.press("Enter");
  await expect(rows(panel)).toHaveCount(3);
  await expect(panel.locator(".es-list")).toBeFocused();
});

test("asks to connect while Studio works on this computer alone", async ({
  page,
}) => {
  const details = await openStepFixture(page, notifyTeam(), async (p) => {
    await routePlatformEvent(p);
    await offline(p);
    await withLanguageFeatures(p, ["flow.eventTrigger"]);
    await p.goto("/");
  });
  await expect(eventTile(page)).toBeAttached();
  const dialog = await openEventTrigger(page, details);
  await startsTab(dialog).click();
  const panel = dialog.getByRole("tabpanel", { name: "Starts" });
  await expect(panel).toContainText(
    "Connect to a platform to see what this trigger started.",
  );
  await expect(panel).toContainText(
    "Starts come from the platform where the workflow is active.",
  );
});

test("a platform without platform event triggers has no Starts tab", async ({
  page,
}) => {
  const { asked } = await openConnected(page, {
    seeds: [CURRENT],
    stored: "t-2",
    features: ["text.concat"],
    pendingStops: [OWED],
  });
  const dialog = await openEventTrigger(page, new StepDetailsPage(page));
  await expect(dialog.getByRole("tab")).toHaveText([/Parameters/, /Settings/]);
  expect(asked).toEqual([]);
});

test("the tab takes its place between Parameters and Settings, by keyboard too, and reads nothing until it is shown", async ({
  page,
}) => {
  const { asked } = await openConnected(page, {
    seeds: [CURRENT],
    stored: "t-2",
  });
  const dialog = await openEventTrigger(page, new StepDetailsPage(page));
  const tabs = dialog
    .getByRole("tablist", { name: "Step details sections" })
    .getByRole("tab");
  await expect(tabs).toHaveText([/Parameters/, /Starts/, /Settings/]);
  expect(asked).toEqual([]);
  await tabs.first().focus();
  await page.keyboard.press("ArrowRight");
  await expect(tabs.nth(1)).toBeFocused();
  await expect(dialog.getByRole("tabpanel", { name: "Starts" })).toContainText(
    "Nothing started yet.",
  );
  await page.keyboard.press("End");
  await expect(tabs.nth(2)).toBeFocused();
  // Coming back reads nothing again.
  const before = asked.length;
  await page.keyboard.press("ArrowLeft");
  await expect(tabs.nth(1)).toHaveAttribute("aria-selected", "true");
  await expect(dialog.getByRole("tabpanel", { name: "Starts" })).toContainText(
    "Nothing started yet.",
  );
  expect(asked).toHaveLength(before);
});

test("a stop still owed comes first, marks the tab, and is made here", async ({
  page,
}) => {
  const { platform } = await openConnected(page, {
    seeds: [CURRENT, OLD],
    stored: "t-2",
    pendingStops: [OWED],
  });
  platform.starts.set("t-2", startedRuns(30));
  // The tab says so before it is opened.
  const { panel, dialog } = await showStarts(page, (tab) =>
    expect(tab).toHaveAccessibleName("Starts, needs attention"),
  );
  await expect(startsTab(dialog)).toHaveAccessibleName(
    "Starts, needs attention",
  );
  const warning = panel.locator(".es-callout");
  await expect(warning).toContainText(
    /Two triggers start notify-team for the same events\.\s*Stop notify-team to keep only notify-team-2\./,
  );
  // First in the panel after the header's Refresh: the warning's Stop it.
  const stop = warning.getByRole("button", { name: "Stop it" });
  await expect(stop).toBeVisible();
  platform.starts.set("t-1", [
    requested("s-1"),
    requested("s-2"),
    requested("s-3"),
  ]);
  await stop.click();
  await expect(warning).toContainText(
    /notify-team has 3 events waiting to start\. Stopping it now skips them\.\s*Keep it for now and both start notify-team for every new event until you stop it\./,
  );
  const keep = warning.getByRole("button", { name: "Keep it for now" });
  await expect(keep).toBeFocused();
  await keep.click();
  await expect(warning).toContainText(
    "Stop notify-team to keep only notify-team-2.",
  );
  await expect(warning.getByRole("button", { name: "Stop it" })).toBeFocused();
  await page.keyboard.press("Enter");
  await warning.getByRole("button", { name: "Stop now" }).click();
  await expect(panel.locator(".es-callout")).toContainText(
    "Stopped notify-team. Only notify-team-2 starts notify-team now.",
  );
  await expect(
    dialog.locator("p.sr-only[aria-live='polite']").first(),
  ).toHaveText(
    "Stopped notify-team. Only notify-team-2 starts notify-team now.",
  );
  await expect(startsTab(dialog)).toHaveAccessibleName("Starts");
  // Focus goes to the list, without scrolling what was said away.
  await expect(panel.locator(".es-list")).toBeFocused();
  await expect(panel.locator(".es-callout")).toBeInViewport({ ratio: 1 });
  expect(platform.sent.at(-1)).toMatchObject({
    key: "POST /event-triggers/t-1/disable",
    revision: 3,
  });
  await panel.getByRole("button", { name: "Refresh the starts" }).click();
  await expect(panel.locator(".es-callout")).toHaveCount(0);
  await new StepDetailsPage(page).close();
  const kept = await storedIntent(page);
  expect(kept).toMatchObject({ resources: { development: "t-2" } });
  expect(kept).not.toHaveProperty("pendingStops");
});

test("a stop still owed for a trigger of other events names the events it ends", async ({
  page,
}) => {
  const { panel } = await open(page, {
    seeds: [
      CURRENT,
      { ...OLD, events: ["transaction.completed"], sources: ["sync-records"] },
    ],
    pendingStops: [OWED],
  });
  const warning = panel.locator(".es-callout");
  const stop = warning.getByRole("button", { name: "Stop it" });
  await expect(stop).toHaveAccessibleDescription(
    "Trigger notify-team also starts this workflow, for other events. It reacts to Completed · from sync-records. Stop it, and those events no longer start this workflow.",
  );
  await stop.click();
  await expect(panel.locator(".es-callout")).toContainText(
    "Stopped notify-team. notify-team-2 still starts this workflow.",
  );
});

test("a stop refused or a trigger changed says so, and focus goes to the way on", async ({
  page,
}) => {
  const { platform, panel } = await open(page, {
    seeds: [CURRENT, OLD],
    pendingStops: [OWED],
  });
  platform.refuse.set(
    "POST /event-triggers/t-1/disable",
    refusal(503, "WV-UNAVAILABLE", "The platform didn't answer in time."),
  );
  const warning = panel.locator(".es-callout");
  await warning.getByRole("button", { name: "Stop it" }).click();
  await expect(warning).toContainText(
    /Couldn't stop notify-team: The platform didn't answer in time\.\s*Both start notify-team for every new event until you stop it\./,
  );
  await expect(warning.getByRole("button", { name: "Stop it" })).toBeFocused();
  platform.find("t-1")!["revision"] = 9;
  await page.keyboard.press("Enter");
  await expect(warning).toContainText(
    /notify-team changed since you opened this\. Review it\.\s*Studio stopped nothing\. Refresh the starts to see what it is now\./,
  );
  const refresh = warning.getByRole("button", { name: "Refresh", exact: true });
  await expect(refresh).toBeFocused();
  await refresh.click();
  await expect(panel.locator(".es-callout")).toHaveCount(0);
  expect(platform.find("t-1")!["disabled"]).toBe(false);
});

/** The next stop's answer waits until `release`. */
async function heldStop(page: Page) {
  const held = { asked: false, release: () => undefined as void };
  const answer = new Promise<void>((resolve) => (held.release = resolve));
  await page.route("**/event-triggers/t-1/disable", async (route) => {
    held.asked = true;
    await answer;
    await route.fallback();
  });
  return held;
}

test("while a stop runs, Refresh waits for it", async ({ page }) => {
  const { panel } = await open(page, {
    seeds: [CURRENT, OLD, { ...OLD, id: "t-3", name: "notify-team-3" }],
    pendingStops: [OWED, { ...OWED, triggerId: "t-3" }],
  });
  const held = await heldStop(page);
  const warning = panel.locator(".es-callout");
  await warning.getByRole("button", { name: "Stop it" }).click();
  await expect.poll(() => held.asked).toBe(true);
  const refresh = panel.getByRole("button", { name: "Refresh the starts" });
  await expect(refresh).toBeDisabled();
  await refresh.focus();
  await page.keyboard.press("Enter");
  await expect(warning).toContainText("Stopping notify-team…");
  held.release();
  await expect(warning).toContainText(
    "Stop notify-team-3 to keep only notify-team-2.",
  );
  await expect(refresh).toBeEnabled();
  await warning.getByRole("button", { name: "Stop it" }).click();
  await expect(warning).toContainText(
    "Stopped notify-team-3. Only notify-team-2 starts notify-team now.",
  );
});

test("while it refreshes, Stop it waits for it", async ({ page }) => {
  const { panel, platform } = await open(page, {
    seeds: [CURRENT, OLD],
    pendingStops: [OWED],
  });
  const stop = panel
    .locator(".es-callout")
    .getByRole("button", { name: "Stop it" });
  await expect(stop).toBeEnabled();
  const held = { asked: false, release: () => undefined as void };
  const answer = new Promise<void>((resolve) => (held.release = resolve));
  await page.route(/\/event-triggers\/t-2(?:\?.*)?$/, async (route) => {
    held.asked = true;
    await answer;
    await route.fallback();
  });
  await panel.getByRole("button", { name: "Refresh the starts" }).click();
  await expect.poll(() => held.asked).toBe(true);
  await expect(stop).toBeDisabled();
  await stop.focus();
  await page.keyboard.press("Enter");
  held.release();
  await expect(stop).toBeEnabled();
  await expect(stop).toBeFocused();
  expect(platform.writes()).toEqual([]);
  await page.keyboard.press("Enter");
  await expect(panel.locator(".es-callout")).toContainText(
    "Stopped notify-team. Only notify-team-2 starts notify-team now.",
  );
});

test("a stop that answers leaves focus where the person moved it meanwhile", async ({
  page,
}) => {
  const opened = await openConnected(page, {
    seeds: [CURRENT, OLD],
    stored: "t-2",
    pendingStops: [OWED],
  });
  opened.platform.starts.set("t-2", EACH);
  const { panel } = await showStarts(page);
  const held = await heldStop(page);
  await panel
    .locator(".es-callout")
    .getByRole("button", { name: "Stop it" })
    .click();
  await expect.poll(() => held.asked).toBe(true);
  const run = panel.getByRole("button", { name: "Open run 9b1c4e2a" });
  await run.focus();
  held.release();
  await expect(panel.locator(".es-callout")).toContainText(
    "Stopped notify-team.",
  );
  await expect(run).toBeFocused();
});

test("a stop that answers once step details closed is settled, without an error", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    // The mocks answer some reads with 404 (the browser logs those).
    if (message.type() === "error" && !message.text().includes("404"))
      errors.push(message.text());
  });
  const { dialog, panel, platform } = await open(page, {
    seeds: [CURRENT, OLD],
    pendingStops: [OWED],
  });
  const held = await heldStop(page);
  await panel
    .locator(".es-callout")
    .getByRole("button", { name: "Stop it" })
    .click();
  await expect.poll(() => held.asked).toBe(true);
  await new StepDetailsPage(page).close();
  await expect(dialog).toHaveCount(0);
  held.release();
  await expect.poll(() => platform.find("t-1")!["disabled"]).toBe(true);
  await expect
    .poll(async () => (await storedIntent(page))?.["pendingStops"])
    .toBeUndefined();
  expect(errors).toEqual([]);
});

test("a stop owed for a trigger someone already stopped marks nothing once it is read", async ({
  page,
}) => {
  const { panel, dialog } = await open(page, {
    seeds: [CURRENT, { ...OLD, disabled: true }],
    pendingStops: [OWED],
  });
  await expect(panel).toContainText("Nothing started yet.");
  await expect(panel.locator(".es-callout")).toHaveCount(0);
  await expect(startsTab(dialog)).toHaveAccessibleName("Starts");
});

test("the next step's details open on Parameters, not on Starts", async ({
  page,
}) => {
  const { dialog } = await open(page);
  await dialog.getByRole("button", { name: "Next step" }).click();
  const next = page.getByRole("dialog", { name: "Step details: send" });
  await expect(next.getByRole("tab", { name: "Parameters" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(startsTab(next)).toHaveCount(0);
  await expect(
    next.getByRole("tabpanel", { name: "Parameters" }),
  ).toBeVisible();
});

for (const [name, size] of [
  ["desktop", { width: 1440, height: 900 }],
  ["tablet", { width: 768, height: 1024 }],
  ["phone", { width: 360, height: 780 }],
] as const)
  test(`the rows and the warning keep their sizes at a ${name}`, async ({
    page,
  }) => {
    await page.setViewportSize(size);
    const opened = await openConnected(page, {
      declaration: WIDER,
      seeds: [CURRENT, OLD],
      stored: "t-2",
      pendingStops: [OWED],
    });
    const RUNS = startedRuns(40).map((item, index) => ({
      ...item,
      run_id: `${String(index).padStart(8, "0")}-1d2e-4f3a-8b4c-5d6e7f8a9b0c`,
    }));
    opened.platform.starts.set("t-2", [...EACH.slice(0, 4), ...RUNS]);
    const { panel } = await showStarts(page);
    await expect(rows(panel)).toHaveCount(20);
    const phone = size.width < 768;
    const at = await box(panel);
    for (const row of (await rows(panel).all()).slice(0, 6)) {
      const shape = await box(row);
      expect(shape.height).toBeGreaterThanOrEqual(phone ? 64 : 56);
      expect(shape.x + shape.width).toBeLessThanOrEqual(at.x + at.width);
      expect(
        await row.evaluate((item) => item.scrollWidth <= item.clientWidth),
      ).toBe(true);
    }
    const button = panel.getByRole("button", { name: "Open run 9b1c4e2a" });
    const run = await box(button);
    const stop = await box(
      panel.locator(".es-callout").getByRole("button", { name: "Stop it" }),
    );
    if (phone) {
      expect([run.width, run.height]).toEqual([44, 44]);
      expect(stop.height).toBeGreaterThanOrEqual(44);
      await expect(panel.getByText("Updated")).toBeHidden();
    } else {
      expect(run.height).toBe(32);
      expect(stop.height).toBe(32);
      // The times line up, whatever each row's action is.
      const times = await rows(panel)
        .locator("time")
        .evaluateAll((items) =>
          items.slice(0, 6).map((item) => {
            const shape = item.getBoundingClientRect();
            return Math.round(shape.right);
          }),
        );
      expect(new Set(times).size).toBe(1);
    }
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth <=
          document.documentElement.clientWidth,
      ),
    ).toBe(true);
    if (size.width === 1440) {
      // The warning, six rows and Show more without scrolling.
      const sixth = await box(rows(panel).nth(5));
      expect(sixth.y + sixth.height).toBeLessThanOrEqual(900);
    }
  });
