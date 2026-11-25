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
// Activating a version sets up the workflow's platform event trigger: the
// dialog says beforehand what Studio creates, moves, replaces or leaves, who
// the new version reacts to and as whom, then what happened; it asks before a
// stop that would skip waiting events, and a stop it still owes comes back at
// the next activation.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { refusal, requested } from "../event-platform";
import { versionId } from "./activation-po";
import {
  activate,
  activationDialog,
  eventRows,
  openFiles,
  openForActivation,
  publish,
  STORED,
  storedIntent,
  WIDER,
} from "./event-activation-po";

const SECOND = { id: "t-2", name: "notify-team-2", activation: "a-older" };
const CLI = {
  id: "t-cli",
  name: "notify-old",
  activation: "a-older",
  events: ["transaction.completed"],
  sources: ["sync-records"],
};
const box = async (locator: Locator) => (await locator.boundingBox())!;
const toast = (page: Page, text: string) =>
  page.locator(".toast").filter({ hasText: text });
const DISABLE = "POST /event-triggers/t-1/disable";

test("the first activation creates the trigger, and the next one moves it as the person", async ({
  page,
}) => {
  const { platform } = await openForActivation(page);
  let dialog = await activationDialog(page);
  await expect(eventRows(dialog)).toHaveText([
    /^\s*Create platform event trigger notify-team\s*For Needs attention · from move-between-systems\.\s*$/,
  ]);
  await expect(
    dialog.getByRole("listitem", {
      name: "Create platform event trigger notify-team for Needs attention · from move-between-systems",
    }),
  ).toBeVisible();
  await activate(dialog);
  await expect(dialog).toHaveCount(0);
  await expect(toast(page, "Activated notify-team 1.2.0 in")).toBeVisible();
  expect(platform.enabled()).toMatchObject([
    {
      name: "notify-team",
      activation_id: "a-new",
      events: ["transaction.in_doubt"],
      source_workflows: ["move-between-systems"],
      may_reconcile: false,
    },
  ]);
  expect(await storedIntent(page)).toMatchObject({
    resources: { development: "t-made-1" },
  });

  dialog = await activationDialog(page);
  const row = dialog.getByRole("listitem", {
    name: "Move platform event trigger notify-team from 1.2.0 to 1.2.0. Runs start as you after the move, including events waiting to start.",
  });
  await expect(row).toHaveText(
    /^\s*Move platform event trigger notify-team from 1\.2\.0 to 1\.2\.0\s*Runs start as you after the move, including events waiting to start\.\s*$/,
  );
  await activate(dialog);
  await expect(dialog).toHaveCount(0);
  expect(platform.sent.at(-1)).toMatchObject({
    key: "POST /event-triggers/t-made-1/retarget",
    revision: 1,
    body: { activation_id: "a-new" },
  });
});

test("a replace names both what reacts now and the wider workflows, then creates before it stops", async ({
  page,
}) => {
  const { platform, asked } = await openForActivation(page, {
    declaration: WIDER,
    seeds: [STORED],
  });
  const dialog = await activationDialog(page);
  const row = dialog.getByRole("listitem", {
    name: "Replace platform event trigger notify-team: 1.2.0 reacts to Needs attention and Undone from any workflow in this project (now: Needs attention from move-between-systems). Studio creates notify-team-2, then stops notify-team.",
  });
  await expect(row).toContainText(
    "1.2.0 reacts to other events and workflows, so Studio creates notify-team-2, then stops notify-team.",
  );
  await expect(row.getByRole("term")).toHaveText(["Now", "1.2.0"]);
  await expect(row.getByRole("definition")).toHaveText([
    "Needs attention · from move-between-systems",
    "Needs attention and Undone · from any workflow in this project",
  ]);
  // The wider workflows are in warning ink, with the warning icon.
  const [now, next] = await row.getByRole("definition").all();
  await expect(next.locator("weave-icon[data-icon='warning']")).toBeVisible();
  await expect(now.locator("weave-icon")).toHaveCount(0);
  const ink = (locator: Locator) =>
    locator.evaluate((element) => getComputedStyle(element).color);
  expect(await ink(next)).not.toBe(await ink(now));
  await activate(dialog);
  await expect(dialog).toHaveCount(0);
  expect(platform.writes()).toEqual(["POST /event-triggers", DISABLE]);
  expect(asked.at(-1)).toMatchObject({
    path: "/event-triggers/t-1/disable",
    ifMatch: '"3"',
  });
  const kept = await storedIntent(page);
  expect(kept).toMatchObject({ resources: { development: "t-made-1" } });
  expect(kept).not.toHaveProperty("pendingStops");
});

test("a stop that would skip waiting events is asked; Done keeps the old trigger, and it comes back on", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, {
    declaration: WIDER,
    seeds: [STORED, CLI],
  });
  platform.starts.set("t-1", [
    requested("s-1"),
    requested("s-2"),
    requested("s-3"),
  ]);
  let dialog = await activationDialog(page);
  await activate(dialog);
  const asking = eventRows(dialog).filter({ hasText: "Created notify-team-2" });
  await expect(asking).toContainText(
    "Created notify-team-2. notify-team has 3 events waiting to start. Stopping it now skips them.",
  );
  await expect(asking).toContainText(
    "Keep it for now and both start notify-team for every new event until you stop it in Starts or at the next activation.",
  );
  await expect(dialog.locator("#activation-summary")).toHaveText(
    /^\s*Activated in .*development\. 1 trigger needs your choice\.\s*$/,
  );
  await expect(
    toast(page, "Activated notify-team 1.2.0. 1 trigger needs your choice."),
  ).toBeVisible();
  // The safe answer has focus, read with what it is about.
  const keep = asking.getByRole("button", { name: "Keep it for now" });
  await expect(keep).toBeFocused();
  await expect(keep).toHaveAccessibleDescription(
    /^Created notify-team-2\. notify-team has 3 events waiting to start\./,
  );
  await expect(asking.getByRole("button", { name: "Stop now" })).toBeVisible();
  await expect(dialog).toContainText("Done keeps notify-team for now.");
  // What nothing was done to stays in place, without its box.
  await expect(eventRows(dialog).filter({ hasText: "notify-old" })).toHaveText(
    /^\s*notify-old keeps running on 1\.0\.0\s*$/,
  );
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(platform.writes()).toEqual(["POST /event-triggers"]);
  const kept = await storedIntent(page);
  expect(kept).toMatchObject({
    resources: { development: "t-made-1" },
    pendingStops: {
      development: [{ triggerId: "t-1", revision: 3, activationId: "a-old" }],
    },
  });

  // After a reload, from the file the canvas is kept in.
  await page.goto("/");
  await openFiles(page, WIDER, [kept!]);
  await publish(page);
  dialog = await activationDialog(page);
  const duplicate = dialog.getByRole("listitem", {
    name: "Two triggers start notify-team for the same events. Stop notify-team",
  });
  await expect(duplicate).toContainText(
    "Stop trigger notify-team, which an earlier activation left running. Both react until one stops.",
  );
  const stop = duplicate.getByRole("checkbox", { name: "Stop it" });
  await expect(stop).toBeChecked();
  await expect(stop).toHaveAccessibleDescription(
    /^Two triggers start notify-team for the same events\./,
  );
  // With nothing waiting any more, the stop it owed is made.
  platform.starts.delete("t-1");
  await activate(dialog);
  await expect(dialog).toHaveCount(0);
  expect(platform.writes().slice(1)).toEqual([
    "POST /event-triggers/t-made-1/retarget",
    DISABLE,
  ]);
  expect(await storedIntent(page)).not.toHaveProperty("pendingStops");
});

test("Stop now stops it, and Keep it for now leaves the choice answered", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, {
    declaration: WIDER,
    seeds: [STORED],
  });
  platform.starts.set("t-1", [requested("s-1")]);
  let dialog = await activationDialog(page);
  await activate(dialog);
  await dialog.getByRole("button", { name: "Keep it for now" }).click();
  await expect(eventRows(dialog)).toHaveText([
    /^\s*Created notify-team-2\. Kept notify-team for now\.\s*Both start notify-team for every new event until you stop it in Starts or at the next activation\.\s*$/,
  ]);
  await expect(dialog.locator("#activation-summary")).toHaveText(
    /^\s*Activated in .*development\.\s*$/,
  );
  await expect(dialog).not.toContainText("Done keeps");
  // The answered button is gone: focus is on Done.
  const done = dialog.getByRole("button", { name: "Done", exact: true });
  await expect(done).toBeFocused();
  await done.click();
  dialog = await activationDialog(page);
  await activate(dialog);
  await dialog.getByRole("button", { name: "Stop now" }).click();
  await expect(eventRows(dialog)).toHaveText([
    /^\s*Moved notify-team-2 to 1\.2\.0\s*$/,
    /^\s*Stopped notify-team\s*$/,
  ]);
  expect(platform.sent.at(-1)).toMatchObject({ key: DISABLE, revision: 3 });
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  expect(await storedIntent(page)).not.toHaveProperty("pendingStops");
});

test("without the starts' permission it asks before a stop it can't check", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, {
    declaration: WIDER,
    seeds: [STORED],
  });
  platform.refuse.set(
    "GET /event-triggers/t-1/starts",
    refusal(403, "WV-FORBIDDEN", "Access denied"),
  );
  const dialog = await activationDialog(page);
  await activate(dialog);
  await expect(eventRows(dialog)).toContainText([
    "Created notify-team-2. Studio can't check whether notify-team has events waiting to start.",
  ]);
  await expect(eventRows(dialog)).toContainText([
    "Stopping it now skips any that are waiting.",
  ]);
  await dialog.getByRole("button", { name: "Stop now" }).click();
  await expect(eventRows(dialog)).toHaveText([
    /^\s*Replaced by notify-team-2\s*notify-team-2 reacts to Needs attention and Undone from any workflow in this project\. notify-team is stopped\.\s*$/,
  ]);
  expect(platform.enabled().map((item) => item["id"])).toEqual(["t-made-1"]);
});

test("a stop that fails says so with Stop it, which tries once more", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, {
    declaration: WIDER,
    seeds: [STORED],
  });
  platform.refuse.set(
    DISABLE,
    refusal(503, "WV-UNAVAILABLE", "The platform didn't answer in time."),
  );
  const dialog = await activationDialog(page);
  await activate(dialog);
  await expect(eventRows(dialog)).toContainText([
    "Created notify-team-2, but couldn't stop notify-team: The platform didn't answer in time.",
  ]);
  await expect(eventRows(dialog)).toContainText([
    "Both start notify-team for every new event until you stop it in Starts or at the next activation.",
  ]);
  await expect(dialog.locator("#activation-summary")).toContainText(
    "1 trigger needs attention.",
  );
  // The stop it owes is kept even if the dialog is closed here.
  await dialog.getByRole("button", { name: "Stop it" }).click();
  await expect(eventRows(dialog)).toContainText(["Replaced by notify-team-2"]);
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  expect(await storedIntent(page)).not.toHaveProperty("pendingStops");
});

test("a stop still owed is kept with the workflow when the dialog closes", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, {
    declaration: WIDER,
    seeds: [STORED],
  });
  platform.refuse.set(
    DISABLE,
    refusal(503, "WV-UNAVAILABLE", "The platform didn't answer in time."),
  );
  let dialog = await activationDialog(page);
  await activate(dialog);
  await expect(dialog.getByRole("button", { name: "Stop it" })).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toHaveCount(0);
  expect(await storedIntent(page)).toMatchObject({
    pendingStops: { development: [{ triggerId: "t-1" }] },
  });
  dialog = await activationDialog(page);
  await expect(
    eventRows(dialog).getByRole("checkbox", { name: "Stop it" }),
  ).toBeChecked();
});

test("a stop owed for a trigger that is gone is forgotten at the next activation", async ({
  page,
}) => {
  await openForActivation(page, {
    seeds: [STORED],
    pendingStops: [
      { triggerId: "t-gone", revision: 1, activationId: "a-older" },
    ],
  });
  const dialog = await activationDialog(page);
  await expect(eventRows(dialog)).toHaveCount(1);
  await activate(dialog);
  await expect(dialog).toHaveCount(0);
  const kept = await storedIntent(page);
  expect(kept).toMatchObject({ resources: { development: "t-1" } });
  expect(kept).not.toHaveProperty("pendingStops");
});

test("a trigger that may resolve transactions says so, and so does its refusal", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, {
    declaration: WIDER,
    seeds: [{ ...STORED, mayReconcile: true }],
  });
  const dialog = await activationDialog(page);
  await expect(eventRows(dialog)).toContainText([
    "Its runs may resolve transactions, as you after the move. This needs permission to reconcile transactions.",
  ]);
  await activate(dialog);
  await expect(eventRows(dialog)).toContainText([
    /Couldn't create notify-team-2: you need to manage triggers, start runs and read runs and reconcile transactions in development\./,
  ]);
  await expect(eventRows(dialog)).toContainText([
    "Ask an administrator for these permissions, then activate again.",
  ]);
  // Trying again changes nothing while the permission is missing.
  await expect(dialog.getByRole("button", { name: "Retry" })).toHaveCount(0);
  expect(platform.enabled().map((item) => item["id"])).toEqual(["t-1"]);
});

test("Retry after a lost answer adopts the trigger already created", async ({
  page,
}) => {
  const { platform } = await openForActivation(page);
  platform.lose.add("POST /event-triggers");
  const dialog = await activationDialog(page);
  await activate(dialog);
  await expect(eventRows(dialog)).toContainText([
    /^\s*Couldn't create notify-team: .*\s*Retry first checks whether it was created\./,
  ]);
  await dialog.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(eventRows(dialog)).toContainText(["Created notify-team"]);
  expect(platform.enabled()).toHaveLength(1);
  expect(platform.writes()).toEqual(["POST /event-triggers"]);
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  expect(await storedIntent(page)).toMatchObject({
    resources: { development: "t-made-1" },
  });
});

test("Retry keeps focus on its row while it fails again, and Done takes it once nothing is left", async ({
  page,
}) => {
  const { platform } = await openForActivation(page);
  const refuse = () =>
    platform.refuse.set(
      "POST /event-triggers",
      refusal(503, "WV-UNAVAILABLE", "The platform didn't answer in time."),
    );
  refuse();
  const dialog = await activationDialog(page);
  await activate(dialog);
  const retry = dialog.getByRole("button", { name: "Retry", exact: true });
  const done = dialog.getByRole("button", { name: "Done", exact: true });
  await expect(done).toBeFocused();
  refuse();
  await retry.focus();
  await page.keyboard.press("Enter");
  await expect
    .poll(() => platform.writes().filter((key) => key.startsWith("POST")))
    .toHaveLength(2);
  await expect(dialog.locator("[aria-disabled='true']")).toHaveCount(0);
  await expect(retry).toBeFocused();
  // A second Enter tries again: it doesn't close the dialog.
  await page.keyboard.press("Enter");
  await expect(eventRows(dialog)).toContainText(["Created notify-team"]);
  await expect(done).toBeFocused();
});

test("someone who activates at the same time leaves one trigger, and the dialog says so", async ({
  page,
}) => {
  const { platform } = await openForActivation(page);
  const dialog = await activationDialog(page);
  await expect(eventRows(dialog)).toContainText([
    "Create platform event trigger notify-team",
  ]);
  platform.seed({
    id: "t-theirs",
    name: "notify-team",
    activation: "a-old",
  });
  await activate(dialog);
  await expect(eventRows(dialog)).toHaveText([
    /^\s*notify-team was created since you opened this\. Review it\.\s*Studio created nothing\. Close this and activate again to see what it is now\.\s*$/,
  ]);
  await expect(dialog.locator("#activation-summary")).toContainText(
    "1 trigger needs attention.",
  );
  expect(platform.writes()).toEqual([]);
  expect(platform.enabled().map((item) => item["id"])).toEqual(["t-theirs"]);
  // Activating again moves theirs, which is this workflow's.
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  const again = await activationDialog(page);
  await expect(eventRows(again)).toContainText([
    "Move platform event trigger notify-team from 1.1.0 to 1.2.0",
  ]);
});

test("triggers made outside Studio are left as they are, Stop it off", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, {
    stored: null,
    seeds: [
      { ...STORED, id: "t-same", events: ["transaction.completed"] },
      CLI,
    ],
  });
  const dialog = await activationDialog(page);
  const outside = dialog.getByRole("listitem", {
    name: "notify-team was created outside Studio with other events. Studio leaves it as it is.",
  });
  await expect(outside).toContainText(
    "Studio leaves it as it is. Change it with the command line.",
  );
  await expect(outside.locator("weave-icon[data-icon='lock']")).toBeVisible();
  const left = dialog.getByRole("listitem", {
    name: "Platform event trigger notify-old keeps running on 1.0.0",
  });
  await expect(left).toContainText(
    "Made with the command line for Completed · from sync-records.",
  );
  for (const row of [outside, left])
    await expect(
      row.getByRole("checkbox", { name: "Stop it" }),
    ).not.toBeChecked();
  await left.getByRole("checkbox", { name: "Stop it" }).check();
  await activate(dialog);
  await expect(dialog).toHaveCount(0);
  expect(platform.writes()).toEqual(["POST /event-triggers/t-cli/disable"]);
});

test("a trigger someone else moved meanwhile isn't stopped", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, {
    seeds: [STORED, SECOND],
  });
  const dialog = await activationDialog(page);
  await expect(
    dialog.getByRole("listitem", { name: /^Two triggers start notify-team/ }),
  ).toBeVisible();
  platform.find("t-2")!["revision"] = 7;
  await activate(dialog);
  await expect(eventRows(dialog)).toHaveText([
    /^\s*Moved notify-team to 1\.2\.0\s*$/,
    /^\s*notify-team-2 changed since you opened this\. Review it\.\s*Studio stopped nothing\. Close this and activate again to see what it is now\.\s*$/,
  ]);
  expect(platform.writes()).toEqual(["POST /event-triggers/t-1/retarget"]);
});

test("a version without platform events still offers to stop the trigger that keeps running", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, {
    declaration: null,
    seeds: [STORED],
  });
  const dialog = await activationDialog(page);
  const left = dialog.getByRole("listitem", {
    name: "Platform event trigger notify-team keeps running on 1.1.0",
  });
  await expect(left).toContainText(
    "For Needs attention · from move-between-systems.",
  );
  await left.getByRole("checkbox", { name: "Stop it" }).check();
  await activate(dialog);
  await expect(dialog).toHaveCount(0);
  expect(platform.writes()).toEqual([DISABLE]);
});

test("the trigger reacts as the published version does, not as the draft edited since", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, { published: WIDER });
  const dialog = await activationDialog(page);
  await expect(eventRows(dialog)).toContainText([
    "For Needs attention and Undone · from any workflow in this project.",
  ]);
  await activate(dialog);
  await expect(dialog).toHaveCount(0);
  expect(platform.enabled()).toMatchObject([
    { events: ["transaction.compensated", "transaction.in_doubt"] },
  ]);
});

test("the dialog can't be closed while its triggers are set up, so what is owed is kept", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, {
    declaration: WIDER,
    seeds: [STORED],
  });
  platform.refuse.set(
    DISABLE,
    refusal(503, "WV-UNAVAILABLE", "The platform didn't answer in time."),
  );
  let answer = () => undefined as void;
  const held = new Promise<void>((resolve) => (answer = resolve));
  let asked = false;
  await page.route("**/event-triggers/t-1/disable", async (route) => {
    asked = true;
    await held;
    await route.fallback();
  });
  const dialog = await activationDialog(page);
  await activate(dialog);
  await expect.poll(() => asked).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dialog).toContainText("Setting up the triggers…");
  answer();
  await expect(dialog.getByRole("button", { name: "Stop it" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  expect(await storedIntent(page)).toMatchObject({
    pendingStops: { development: [{ triggerId: "t-1" }] },
  });
});

test("nothing leaves the dialog while its triggers are set up, or while a row's answer runs", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, {
    declaration: WIDER,
    seeds: [STORED],
    triggers: [
      {
        id: "trigger-4e5f6a7b",
        kind: "schedule",
        cron: "0 9 * * *",
        input: {},
      },
    ],
  });
  // The published version: an input the schedule's doesn't fit, and an
  // optional slot with no connection to bind.
  await page.route(`**/projects/project/workflows/${versionId}/export`, (r) =>
    r.fulfill({
      json: {
        id: versionId,
        document: {
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "notify-team", version: "1.2.0" },
          spec: {
            inputSchema: {
              type: "object",
              properties: { reference: { type: "string" } },
              required: ["reference"],
            },
            outputSchema: {},
            eventTrigger: WIDER,
            connections: {
              audit: { connector: "weave-http@2.0.0", required: false },
            },
            steps: [],
            output: { literal: null },
          },
        },
        artifact: {
          executable: {
            features: ["flow.eventTrigger"],
            connections: {
              audit: { connector: "weave-http@2.0.0", required: false },
            },
            dependencies: [],
          },
        },
      },
    }),
  );
  const held = { release: () => undefined as void, asked: 0 };
  await page.route("**/event-triggers/t-1/disable", async (route) => {
    held.asked++;
    await new Promise<void>((resolve) => (held.release = resolve));
    await route.fallback();
  });
  const refuse = () =>
    platform.refuse.set(
      DISABLE,
      refusal(503, "WV-UNAVAILABLE", "The platform didn't answer in time."),
    );
  refuse();
  const dialog = await activationDialog(page);
  const schedule = dialog.getByRole("button", { name: "Open schedule" });
  const connections = dialog.getByRole("button", {
    name: "Create one in Connections.",
  });
  await expect(schedule).toBeEnabled();
  await expect(connections).toBeEnabled();
  await activate(dialog);
  const stays = async (asked: number) => {
    await expect.poll(() => held.asked).toBe(asked);
    await expect(schedule).toBeDisabled();
    await expect(connections).toBeDisabled();
  };
  await stays(1);
  held.release();
  const again = dialog.getByRole("button", { name: "Stop it" });
  await expect(again).toBeVisible();
  await expect(schedule).toBeEnabled();
  // The answer that runs again: the same.
  refuse();
  await again.click();
  await stays(2);
  held.release();
  // What it made and what it owes are kept, and the way to the schedule opens.
  await schedule.click();
  await expect(dialog.filter({ hasText: "Activated notify-team" })).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("dialog", { name: "Step details: Schedule trigger" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  expect(await storedIntent(page)).toMatchObject({
    resources: { development: "t-made-1" },
    pendingStops: { development: [{ triggerId: "t-1" }] },
  });
});

test("a trigger of an activation past the list's pages is still this workflow's", async ({
  page,
}) => {
  await openForActivation(page, {
    unlisted: "readable",
    seeds: [{ ...STORED, activation: "a-unlisted" }],
  });
  const dialog = await activationDialog(page);
  await expect(eventRows(dialog)).toContainText([
    "Move platform event trigger notify-team from 1.0.0 to 1.2.0",
  ]);
});

test("nothing is planned for the trigger while its activation can't be read", async ({
  page,
}) => {
  const { platform } = await openForActivation(page, {
    unlisted: "unreadable",
    seeds: [{ ...STORED, activation: "a-unlisted" }],
  });
  const dialog = await activationDialog(page);
  await expect(eventRows(dialog)).toHaveText([
    "Couldn't check the platform event triggers: Try again later.",
  ]);
  await activate(dialog);
  await expect(dialog).toHaveCount(0);
  expect(platform.writes()).toEqual([]);
});

test("a platform without event triggers is never asked for them", async ({
  page,
}) => {
  const { asked } = await openForActivation(page, {
    declaration: null,
    features: [],
  });
  const dialog = await activationDialog(page);
  await activate(dialog);
  await expect(dialog).toHaveCount(0);
  expect(asked).toEqual([]);
});

for (const [name, size, control] of [
  ["desktop", { width: 1440, height: 900 }, 32],
  ["tablet", { width: 768, height: 1024 }, 32],
  ["phone", { width: 360, height: 780 }, 44],
] as const)
  test(`the rows and their answers keep their sizes at a ${name}`, async ({
    page,
  }) => {
    await page.setViewportSize(size);
    const { platform } = await openForActivation(page, {
      declaration: WIDER,
      seeds: [{ ...STORED, mayReconcile: true }, CLI],
    });
    platform.capabilities.add("transaction.reconcile");
    platform.starts.set("t-1", [requested("s-1")]);
    const dialog = await activationDialog(page);
    const inside = async () => {
      const panel = await box(dialog);
      expect(panel.x).toBeGreaterThanOrEqual(0);
      expect(panel.x + panel.width).toBeLessThanOrEqual(size.width);
      for (const row of await eventRows(dialog).all()) {
        const at = await box(row);
        expect(at.height).toBeGreaterThanOrEqual(44);
        expect(at.x).toBeGreaterThanOrEqual(panel.x);
        expect(at.x + at.width).toBeLessThanOrEqual(panel.x + panel.width);
        expect(
          await row.evaluate((item) => item.scrollWidth <= item.clientWidth),
        ).toBe(true);
      }
    };
    await inside();
    const row = eventRows(dialog).first();
    const [term, definition] = [
      await box(row.getByRole("term").first()),
      await box(row.getByRole("definition").first()),
    ];
    // Two columns, or on a phone the label above its line.
    if (size.width < 768)
      expect(definition.y).toBeGreaterThanOrEqual(term.y + term.height - 1);
    else expect(definition.x).toBeGreaterThanOrEqual(term.x + term.width);
    const stop = await box(dialog.locator(".activation-stop-it"));
    expect(stop.height).toBeGreaterThanOrEqual(44);
    const tick = await box(dialog.getByRole("checkbox", { name: "Stop it" }));
    expect([tick.width, tick.height]).toEqual([16, 16]);
    // Beside the text, the label takes no more than its own height.
    if (size.width >= 768)
      expect((await box(eventRows(dialog).last())).height).toBe(
        stop.height + 20,
      );
    await activate(dialog);
    const keep = dialog.getByRole("button", { name: "Keep it for now" });
    await expect(keep).toBeFocused();
    await inside();
    const [first, second] = [
      await box(keep),
      await box(dialog.getByRole("button", { name: "Stop now" })),
    ];
    expect(first.height).toBeGreaterThanOrEqual(control);
    expect(second.height).toBeGreaterThanOrEqual(control);
    // The two answers side by side, under the text on a phone.
    expect(Math.abs(first.y - second.y)).toBeLessThan(1);
    const lead = await box(eventRows(dialog).first().locator(".row-lead"));
    if (size.width < 768)
      expect(first.y).toBeGreaterThanOrEqual(lead.y + lead.height);
    else expect(first.x).toBeGreaterThanOrEqual(lead.x + lead.width);
  });
