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
// Resolving a transaction in doubt from its drawer: the decisions offered for
// its code and to the person, read again before one that says how it ended,
// what each dialog asks for before it can be sent, what is sent, and how each
// refusal reads. The platform is mocked with its own codes.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page, type Route } from "@playwright/test";
import type { IncidentView } from "../../src/app/operate/incidents/incidents-model";
import {
  called,
  caller,
  held,
  ids,
  loop,
  others,
  runs,
  unknown,
} from "../transaction-incidents-fixture";
import {
  decisions,
  environment,
  incidentPlatform,
  resolveIncident,
  type IncidentPlatform,
} from "./incidents-po";

const captures = resolve(
  process.env["STUDIO_OPERATE_CAPTURES"]?.trim() || "test-results/operate",
);
mkdirSync(captures, { recursive: true });

const operator = ["incident.read", "incident.resolve", "run.read"];
const reconciler = [...operator, "transaction.reconcile"];
const uuid =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

const drawer = (page: Page) => page.locator("weave-incident-drawer");
const title = (page: Page) => drawer(page).locator("#incident-detail-title");
const offers = (page: Page) =>
  drawer(page).getByRole("region", { name: "Resolve this incident" });
const offer = (page: Page, name: string) =>
  offers(page).getByRole("button", { name, exact: true });
const dialog = (page: Page, name: string) =>
  page.getByRole("dialog", { name, exact: true });
const confirm = (page: Page, name: string) =>
  dialog(page, name).getByRole("button", { name, exact: true });
const field = (page: Page, name: string, label: string) =>
  dialog(page, name).getByLabel(label);
/** One of Mark as reconciled's two endings. */
const ending = (page: Page, name: string, label: string) =>
  dialog(page, name).getByRole("radio", { name: new RegExp(`^${label}`) });
/** What a screen reader reads of a control beside its name. */
const described = (page: Page, name: string) =>
  confirm(page, name).evaluate((button) =>
    (button.getAttribute("aria-describedby") ?? "")
      .split(" ")
      .map((id) => document.getElementById(id)?.textContent?.trim() ?? "")
      .join(" ")
      .trim(),
  );
const open = async (page: Page, id: string) => {
  await page.goto(`/operate/incidents?incident=${id}`);
  await expect(title(page)).toBeVisible();
};
/** A platform with one of the fixture's incidents open, as a reconciler by default. */
async function opened(
  page: Page,
  incident: IncidentView,
  options: Parameters<typeof incidentPlatform>[1] = {},
): Promise<IncidentPlatform> {
  const platform = await incidentPlatform(page, {
    capabilities: reconciler,
    items: [incident, ...others],
    ...options,
  });
  await open(page, incident.id);
  // What a decision reads is what is asked for after the drawer opened.
  await expect(drawer(page).locator(".event-lines li").first()).toBeVisible();
  platform.reads.length = 0;
  return platform;
}
/** Puts a changed copy of an incident on the platform. */
const change = (platform: IncidentPlatform, next: IncidentView) => {
  platform.items = platform.items.map((item) =>
    item.id === next.id ? next : item,
  );
};
/** The incident once its step was tried again: another revision, another record. */
const triedAgain = (
  incident: IncidentView,
  attempts: number,
): IncidentView => ({
  ...incident,
  revision: incident.revision + 1,
  transaction: {
    ...incident.transaction!,
    participants: incident.transaction!.participants.map((participant) =>
      participant.status === "undo_stuck"
        ? { ...participant, attempts }
        : participant,
    ),
  },
});
const refuse = (route: Route, status: number, code: string, message: string) =>
  route.fulfill({ status, json: { code, message } });
const drawn = (page: Page) =>
  page.evaluate(
    () =>
      new Promise<void>((done) =>
        requestAnimationFrame(() => requestAnimationFrame(() => done())),
      ),
  );
/** Opens Mark as reconciled on the called run's incident, filled in as undone. */
async function undone(page: Page) {
  const name = "Mark as reconciled";
  await offer(page, name).click();
  await ending(page, name, "Undone").check();
  await dialog(page, name)
    .getByRole("checkbox", {
      name: "I confirmed add was reversed in the other system.",
    })
    .check();
  await field(page, name, "Reason").fill("The quantity is back");
  await field(page, name, "Evidence reference").fill("OPS-2304");
}

test.use({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC" });

// ---------------------------------------------------------------- offers

for (const [incident, labels] of [
  [called, ["Retry the undo", "Mark as reconciled", "Force end"]],
  [loop, ["Retry the release", "Mark as reconciled", "Force end"]],
  [caller, ["Wait again", "Mark as reconciled", "Force end"]],
  [unknown, ["It happened", "It didn't happen", "Force end"]],
  // A run held whole names no transaction, only its priority.
  [held, ["Force end"]],
] as const)
  test(`${incident.code} offers ${labels.join(", ")}, none as the recommended one`, async ({
    page,
  }) => {
    await opened(page, incident);
    await expect(offers(page).getByRole("button")).toHaveText([...labels]);
    for (const button of await offers(page).getByRole("button").all()) {
      await expect(button).toBeEnabled();
      await expect(button).not.toHaveClass(/primary/);
    }
    await expect(offer(page, "Force end")).toHaveClass(/danger/);
    // The plain dialog, with its retry and its verified result, never opens for one.
    await expect(
      drawer(page).getByRole("button", { name: "Resolve incident" }),
    ).toHaveCount(0);
    const order = await drawer(page).evaluate((element) =>
      [".txn-panel", ".offers", ".recent-events"].map((selector) =>
        Math.round(
          element.querySelector(selector)!.getBoundingClientRect().top,
        ),
      ),
    );
    expect(order).toEqual([...order].sort((a, b) => a - b));
    await offers(page).scrollIntoViewIfNeeded();
    await page.screenshot({
      path: `${captures}/offers-${incident.code}-1440.png`,
    });
  });

test("an incident that isn't in doubt keeps Resolve incident, and a decided one offers nothing", async ({
  page,
}) => {
  const platform = await opened(page, others[0]);
  await expect(offers(page)).toHaveCount(0);
  await drawer(page).getByRole("button", { name: "Resolve incident" }).click();
  await expect(dialog(page, "Resolve incident")).toBeVisible();
  await page.keyboard.press("Escape");
  platform.items = [{ ...called, status: "resolved", revision: 2 }];
  await open(page, ids.called);
  await expect(title(page)).toHaveText("Incident WV-TXN-UNDO-STUCK");
  await expect(offers(page)).toHaveCount(0);
  await expect(
    drawer(page).getByRole("button", { name: "Resolve incident" }),
  ).toHaveCount(0);
});

test("an operator without the role reads why, and can only retry", async ({
  page,
}) => {
  await opened(page, called, { capabilities: operator });
  await expect(offer(page, "Retry the undo")).toBeEnabled();
  for (const name of ["Mark as reconciled", "Force end"]) {
    await expect(offer(page, name)).toBeDisabled();
    const why = offers(page).locator(".offer", {
      has: page.getByRole("button", { name, exact: true }),
    });
    await expect(why).toContainText("Needs the transaction reconciler role.");
    await expect(why.locator("weave-icon[name=lock]")).toBeVisible();
  }
  await offers(page).scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${captures}/offers-no-role-1440.png` });
});

test("a person who can't resolve incidents reads why, and no button sends", async ({
  page,
}) => {
  await opened(page, called, {
    capabilities: ["incident.read", "run.read", "transaction.reconcile"],
  });
  for (const button of await offers(page).getByRole("button").all())
    await expect(button).toBeDisabled();
  await expect(
    offers(page).getByText("Needs permission to resolve incidents."),
  ).toHaveCount(3);
});

test("a grant for this incident alone isn't enough: the platform asks for the environment's", async ({
  page,
}) => {
  const scope = {
    tenant_id: "tenant",
    project_id: "project",
    environment_id: "development",
  };
  await incidentPlatform(page, { items: [called] });
  await page.route("**/studio/api/api/v1/identity", (route) =>
    route.fulfill({
      json: {
        principal_id: "human",
        kind: "human",
        grants: [
          { role: "reader", scope, resources: [], capabilities: operator },
          {
            role: "transaction_reconciler",
            scope,
            resources: [ids.called],
            capabilities: ["transaction.reconcile"],
          },
        ],
        workspaces: [],
        truncated: false,
      },
    }),
  );
  await open(page, ids.called);
  await expect(offer(page, "Retry the undo")).toBeEnabled();
  await expect(offer(page, "Mark as reconciled")).toBeDisabled();
  await expect(offer(page, "Force end")).toBeDisabled();
});

// --------------------------------------------------- reading it again first

test("Mark as reconciled reads the incident first, once, and opens on what it read", async ({
  page,
}) => {
  const platform = await opened(page, called);
  const sent = await decisions(page, platform);
  const reads: Route[] = [];
  await page.route(`${environment}/incidents/${ids.called}`, (route) => {
    reads.push(route);
  });
  const name = "Mark as reconciled";
  await offer(page, name).focus();
  // Two presses in one turn, as a double click sends them.
  await offer(page, name).evaluate((button: HTMLElement) => {
    button.click();
    button.click();
  });
  const reading = offer(page, "Reading the incident…");
  await expect(reading).toBeFocused();
  await expect(reading).toHaveAttribute("aria-busy", "true");
  await page.screenshot({ path: `${captures}/offer-reading-1440.png` });
  await reading.evaluate((button: HTMLElement) => button.click());
  expect(reads).toHaveLength(1);
  await expect(dialog(page, name)).toHaveCount(0);
  await reads[0].fulfill({ json: triedAgain(called, 4) });
  await expect(dialog(page, name)).toBeVisible();
  await ending(page, name, "Undone").check();
  // The checklist is the record's it read, and so is the revision it decides on.
  await expect(dialog(page, name)).toContainText("Undo failed after 4 tries");
  await page.unroute(`${environment}/incidents/${ids.called}`);
  change(platform, triedAgain(called, 4));
  await dialog(page, name).getByRole("checkbox").check();
  await field(page, name, "Reason").fill("The quantity is back");
  await field(page, name, "Evidence reference").fill("OPS-2304");
  await confirm(page, name).click();
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0].revision).toBe('"2"');
  expect(sent[0].body).toEqual({
    receipt_id: expect.stringMatching(uuid) as unknown,
    kind: "accept_reconciled_result",
    reason: "The quantity is back",
    evidence_reference: "OPS-2304",
    output: {
      as: "compensated",
      participants: [{ instance_key: "add", outcome: "undone" }],
    },
  });
});

for (const [name, answer, words, code] of [
  [
    "couldn't be read",
    { status: 503, json: { code: "WV-UNAVAILABLE", message: "Unavailable" } },
    "Studio couldn't read this incident. Choose again to read it.",
    "Support code: WV-UNAVAILABLE",
  ],
  [
    "can't be found",
    {
      status: 404,
      json: { code: "WV-NOT-FOUND", message: "Incident not found" },
    },
    "This incident can't be found. It may belong to another environment, or its run's data was deleted.",
    "Support code: WV-NOT-FOUND",
  ],
  [
    "is refused",
    { status: 403, json: { code: "WV-FORBIDDEN", message: "Not allowed" } },
    "You need incident.read in this environment. Ask an administrator in Settings › People and access.",
    "Support code: WV-FORBIDDEN",
  ],
  [
    "can't be shown",
    { json: { id: ids.called, unavailable: true } },
    "Studio can't show this incident: its run can't be shown here.",
    "",
  ],
] as const)
  test(`no dialog opens on an incident that ${name}; its offer says why and reads it again`, async ({
    page,
  }) => {
    await opened(page, called);
    let failing = true;
    await page.route(`${environment}/incidents/${ids.called}`, (route) =>
      failing ? route.fulfill(answer) : route.fallback(),
    );
    await offer(page, "Force end").focus();
    await page.keyboard.press("Enter");
    const alert = offers(page).getByRole("alert");
    await expect(alert).toContainText(words);
    await expect(alert).toContainText(code);
    await expect(offer(page, "Force end")).toBeFocused();
    await expect(dialog(page, "Force end")).toHaveCount(0);
    failing = false;
    await page.keyboard.press("Enter");
    await expect(dialog(page, "Force end")).toBeVisible();
    await expect(alert).toHaveCount(0);
  });

test("an incident decided meanwhile opens no dialog and says so", async ({
  page,
}) => {
  const platform = await opened(page, called);
  change(platform, {
    ...called,
    status: "resolved",
    revision: 2,
    resolved_at: "2026-11-22T10:53:00Z",
    resolution: {
      receipt_id: "93333333-3333-4333-8333-333333333333",
      kind: "retry_safe",
      reason: "The target system is back",
    },
  });
  await offer(page, "Mark as reconciled").click();
  await expect(
    page.getByText("Incident WV-TXN-UNDO-STUCK at add is no longer active."),
  ).toBeVisible();
  await expect(drawer(page).locator("dt:text-is('Decision') + dd")).toHaveText(
    "Retry the undo",
  );
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(title(page)).toBeFocused();
});

test("a dialog never opens for an incident the person left, nor over one asked for after", async ({
  page,
}) => {
  await opened(page, called, { items: [called, unknown, ...others] });
  const reads: Route[] = [];
  await page.route(`${environment}/incidents/${ids.called}`, (route) => {
    reads.push(route);
  });
  await offer(page, "Force end").click();
  await expect.poll(() => reads.length).toBe(1);
  await page
    .getByRole("button", {
      name: "Open incident WV-TXN-PIVOT-UNKNOWN at submit",
    })
    .click();
  await reads[0].fulfill({ json: called });
  await drawn(page);
  await expect(title(page)).toHaveText("Incident WV-TXN-PIVOT-UNKNOWN");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(offer(page, "Force end")).toBeEnabled();
  // Back on it, its offer reads again as if never pressed.
  await page
    .getByRole("button", { name: "Open incident WV-TXN-UNDO-STUCK at add" })
    .click();
  await expect(offer(page, "Force end")).toBeEnabled();
  await offer(page, "Force end").click();
  await expect.poll(() => reads.length).toBe(2);
  // The last decision pressed is the one that opens.
  await offer(page, "Mark as reconciled").click();
  await expect.poll(() => reads.length).toBe(3);
  await reads[1].fulfill({ json: called });
  await drawn(page);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await reads[2].fulfill({ json: called });
  await expect(dialog(page, "Mark as reconciled")).toBeVisible();
});

for (const [incident, name] of [
  [called, "Retry the undo"],
  [others[0], "Resolve incident"],
] as const)
  test(`an incident the address asked for never opens under ${name}`, async ({
    page,
  }) => {
    await opened(page, incident);
    const reads: Route[] = [];
    await page.route(`${environment}/incidents/${ids.caller}`, (route) => {
      reads.push(route);
    });
    await page.evaluate((id) => {
      history.pushState(null, "", `/operate/incidents?incident=${id}`);
      dispatchEvent(new PopStateEvent("popstate"));
    }, ids.caller);
    await expect.poll(() => reads.length).toBe(1);
    await drawer(page).getByRole("button", { name, exact: true }).click();
    await field(page, name, "Reason").fill("The system is back");
    await reads[0].fulfill({ json: caller });
    await drawn(page);
    await expect(field(page, name, "Reason")).toHaveValue("The system is back");
    await expect(title(page)).toHaveText(`Incident ${incident.code}`);
    await expect(page).toHaveURL(new RegExp(`incident=${incident.id}$`));
  });

test("a refresh of the list never changes the incident under a dialog", async ({
  page,
}) => {
  const platform = await opened(page, called);
  const sent = await decisions(page, platform);
  await undone(page);
  change(platform, triedAgain(called, 9));
  const queries = platform.queries.length;
  await page.clock.fastForward(40_000);
  await drawn(page);
  expect(platform.queries).toHaveLength(queries);
  await expect(dialog(page, "Mark as reconciled")).toContainText(
    "Undo failed after 3 tries",
  );
  await confirm(page, "Mark as reconciled").click();
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0].revision).toBe('"1"');
});

// -------------------------------------------------------------- the dialogs

test("Retry the undo sends retry_safe on the drawer's revision, without reading it again", async ({
  page,
}) => {
  const platform = await opened(page, called);
  const sent = await decisions(page, platform);
  const name = "Retry the undo";
  await offer(page, name).click();
  const asked = dialog(page, name);
  await expect(asked.getByRole("heading", { name })).toBeFocused();
  await expect(asked).toContainText(
    "Runs the undo again with the same operation key. Only if the other system can take it again.",
  );
  await expect(asked).toContainText("Step add in run 9a01c7d2");
  expect(platform.reads).toEqual([]);
  await expect(confirm(page, name)).toHaveAttribute("aria-disabled", "true");
  await expect
    .poll(() => described(page, name))
    .toBe("To retry the undo, fill in Reason.");
  // It can be reached, and does nothing until it has what it needs.
  await confirm(page, name).evaluate((button: HTMLElement) => button.click());
  await field(page, name, "Reason").fill("The target system is back");
  await expect(asked).toContainText("25 / 2,000");
  await expect(confirm(page, name)).not.toHaveAttribute("aria-disabled");
  await expect.poll(() => described(page, name)).toBe("");
  await page.screenshot({ path: `${captures}/retry-1440.png` });
  await confirm(page, name).click();
  await expect(asked).toHaveCount(0);
  expect(sent).toHaveLength(1);
  expect(sent[0].revision).toBe('"1"');
  expect(sent[0].body).toEqual({
    receipt_id: expect.stringMatching(uuid) as unknown,
    kind: "retry_safe",
    reason: "The target system is back",
  });
  await expect(page.getByText("Incident resolved.")).toBeVisible();
  await expect(title(page)).toBeFocused();
});

test("Mark as reconciled names only what it still lacks, and drops the ticks of another ending", async ({
  page,
}) => {
  await opened(page, called);
  const name = "Mark as reconciled";
  await offer(page, name).click();
  const asked = dialog(page, name);
  await expect(asked).toContainText(
    "You checked every step in the other systems. Say how the transaction ended.",
  );
  await expect(asked).toContainText("Transaction apply in run 9a01c7d2");
  for (const label of ["Undone", "Finished"])
    await expect(ending(page, name, label)).not.toBeChecked();
  await expect(asked.getByRole("checkbox")).toHaveCount(0);
  await expect
    .poll(() => described(page, name))
    .toBe(
      "To mark it as reconciled, choose how it ended, fill in Reason and fill in Evidence reference.",
    );
  await ending(page, name, "Undone").check();
  await expect(asked).toContainText("Check each step in its system");
  await expect(asked).toContainText(
    `Undo failed after 3 tries · operation key ${runs.called}:add#undo`,
  );
  await expect(asked).toContainText(
    "Only the steps Weave couldn't finish are listed. Weave already knows how the others ended.",
  );
  await expect(asked.getByLabel("Result of the transaction")).toHaveCount(0);
  await field(page, name, "Reason").fill(
    "Checked the target system: the quantity is back to 120.",
  );
  await expect
    .poll(() => described(page, name))
    .toBe(
      "To mark it as reconciled, tick the line for add and fill in Evidence reference.",
    );
  await expect(asked.locator(".confirm-reason code")).toHaveText("add");
  await page.screenshot({ path: `${captures}/reconcile-undone-1440.png` });
  const reversed = asked.getByRole("checkbox", {
    name: "I confirmed add was reversed in the other system.",
  });
  await reversed.check();
  await expect
    .poll(() => described(page, name))
    .toBe("To mark it as reconciled, fill in Evidence reference.");
  await ending(page, name, "Finished").check();
  // Another ending asks another question of each step.
  await expect(
    asked.getByRole("checkbox", {
      name: "I confirmed add finished in the other system.",
    }),
  ).not.toBeChecked();
  await expect
    .poll(() => described(page, name))
    .toBe(
      "To mark it as reconciled, tick the line for add, fill in Result of the transaction and fill in Evidence reference.",
    );
  await ending(page, name, "Undone").check();
  await expect(reversed).not.toBeChecked();
});

test("Mark as reconciled as finished sends the result, and never one it can't read", async ({
  page,
}) => {
  const platform = await opened(page, called);
  const sent = await decisions(page, platform);
  const name = "Mark as reconciled";
  await offer(page, name).click();
  await ending(page, name, "Finished").check();
  const asked = dialog(page, name);
  await asked.getByRole("checkbox").check();
  await field(page, name, "Reason").fill("The 40 were added at 10:36");
  await field(page, name, "Evidence reference").fill("Log line 90412");
  const result = field(page, name, "Result of the transaction (JSON)");
  await expect(asked).toContainText(
    "What the transaction returns to the steps after it, as its output says.",
  );
  await result.fill("{quantity: 40}");
  await expect(result).not.toHaveAttribute("aria-invalid");
  await confirm(page, name).click();
  await expect(result).toHaveAttribute("aria-invalid", "true");
  await expect(asked).toContainText(
    'Enter the result as JSON, for example {"quantity": 40}.',
  );
  await expect(result).toBeFocused();
  expect(sent).toEqual([]);
  await result.fill('{"quantity": 40}');
  await expect(result).not.toHaveAttribute("aria-invalid");
  await confirm(page, name).click();
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0].body.output).toEqual({
    as: "completed",
    participants: [{ instance_key: "add", outcome: "done" }],
    blockOutput: { quantity: 40 },
  });
  await expect(drawer(page).locator("dt:text-is('Decision') + dd")).toHaveText(
    "Mark as reconciled as finished",
  );
});

test("a result typed for a finished transaction is never sent with one that was undone", async ({
  page,
}) => {
  const platform = await opened(page, called);
  const sent = await decisions(page, platform);
  const name = "Mark as reconciled";
  await offer(page, name).click();
  await ending(page, name, "Finished").check();
  await field(page, name, "Result of the transaction (JSON)").fill("{}");
  await ending(page, name, "Undone").check();
  await dialog(page, name).getByRole("checkbox").check();
  await field(page, name, "Reason").fill("It is back");
  await field(page, name, "Evidence reference").fill("OPS-2304");
  await confirm(page, name).click();
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0].body.output).toEqual({
    as: "compensated",
    participants: [{ instance_key: "add", outcome: "undone" }],
  });
});

test("a settle timeout has no stuck step: Mark as reconciled asks for no checklist", async ({
  page,
}) => {
  await opened(page, caller);
  const name = "Mark as reconciled";
  await offer(page, name).click();
  await ending(page, name, "Undone").check();
  await expect(dialog(page, name).getByRole("checkbox")).toHaveCount(0);
  await expect(dialog(page, name)).not.toContainText("Check each step");
  await expect
    .poll(() => described(page, name))
    .toBe(
      "To mark it as reconciled, fill in Reason and fill in Evidence reference.",
    );
});

test("evidence holding markup is text in the drawer once resolved", async ({
  page,
}) => {
  const platform = await opened(page, called);
  await decisions(page, platform);
  await undone(page);
  const markup = '<b>x</b><img src=x onerror="document.title=1">';
  await field(page, "Mark as reconciled", "Evidence reference").fill(markup);
  await field(page, "Mark as reconciled", "Reason").fill("<i>y</i>");
  await confirm(page, "Mark as reconciled").click();
  await expect(drawer(page).locator("dt:text-is('Evidence') + dd")).toHaveText(
    markup,
  );
  await expect(drawer(page).locator("dt:text-is('Reason') + dd")).toHaveText(
    "<i>y</i>",
  );
  await expect(drawer(page).locator("dd :is(b, i, img)")).toHaveCount(0);
});

for (const [name, kind, output] of [
  ["It happened", "accept_reconciled_result", { order: "O-77310" }],
  ["It didn't happen", "accept_reconciled_failure", undefined],
] as const)
  test(`${name} sends ${kind} with its evidence${output ? " and the step's output" : ""}`, async ({
    page,
  }) => {
    const platform = await opened(page, unknown);
    const sent = await decisions(page, platform);
    await offer(page, name).click();
    const asked = dialog(page, name);
    await expect(asked).toContainText("Step submit in run 7b3e9d10");
    expect(platform.reads).toEqual([ids.unknown]);
    const lead =
      name === "It happened"
        ? "To record that it happened, fill in Result of the step, fill in Reason"
        : "To record that it didn't happen, fill in Reason";
    await expect
      .poll(() => described(page, name))
      .toBe(`${lead} and fill in Evidence reference.`);
    const result = asked.getByLabel("Result of the step (JSON)");
    await expect(result).toHaveCount(output ? 1 : 0);
    if (output) {
      await expect(asked).toContainText(
        "What submit would have returned. The steps after it read this.",
      );
      await result.fill(JSON.stringify(output));
    }
    await field(page, name, "Reason").fill("Searched the order system");
    await field(page, name, "Evidence reference").fill("Search at 10:52");
    await page.screenshot({
      path: `${captures}/${output ? "it-happened" : "it-didnt-happen"}-1440.png`,
    });
    await confirm(page, name).click();
    await expect.poll(() => sent.length).toBe(1);
    expect(sent[0].body).toEqual({
      receipt_id: expect.stringMatching(uuid) as unknown,
      kind,
      reason: "Searched the order system",
      evidence_reference: "Search at 10:52",
      ...(output ? { output } : {}),
    });
    await expect(
      drawer(page).locator("dt:text-is('Decision') + dd"),
    ).toHaveText(name);
  });

test("Force end lists what stays done in the record just read, and needs the run's ID typed", async ({
  page,
}) => {
  const platform = await opened(page, unknown);
  const sent = await decisions(page, platform);
  // Meanwhile a step was released: the drawer's record is older.
  change(platform, {
    ...unknown,
    revision: 7,
    transaction: {
      ...unknown.transaction!,
      participants: unknown.transaction!.participants.map((participant) =>
        participant.node_id === "hold"
          ? { ...participant, status: "cancelled" }
          : participant,
      ),
    },
  });
  const name = "Force end";
  await offer(page, name).click();
  const asked = dialog(page, name);
  await expect(asked).toContainText("Transaction move and run 7b3e9d10");
  await expect(asked.getByRole("listitem")).toHaveText([
    "add · Reversible: Weave won't undo it",
    "submit · Can't be undone: it may or may not have happened",
  ]);
  await expect
    .poll(() => described(page, name))
    .toBe("To force end it, fill in Reason and type the run ID.");
  await field(page, name, "Reason").fill("Closed by hand (OPS-2310)");
  const typed = field(page, name, "Type 7b3e9d10 to confirm");
  await expect(typed).toHaveValue("");
  for (const wrong of ["", "7b3e9d1", "9a01c7d2"]) {
    await typed.fill(wrong);
    await typed.press("Enter");
    await expect(confirm(page, name)).toHaveAttribute("aria-disabled", "true");
    await expect
      .poll(() => described(page, name))
      .toBe("To force end it, type the run ID.");
  }
  await typed.fill(" 7B3E9D10 ");
  await page.screenshot({ path: `${captures}/force-end-1440.png` });
  await confirm(page, name).click();
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0].revision).toBe('"7"');
  expect(sent[0].body).toEqual({
    receipt_id: expect.stringMatching(uuid) as unknown,
    kind: "terminate",
    reason: "Closed by hand (OPS-2310)",
  });
});

test("a run held whole can only be force ended, whatever its record", async ({
  page,
}) => {
  const platform = await opened(page, held);
  const sent = await decisions(page, platform);
  const name = "Force end";
  await offer(page, name).click();
  const asked = dialog(page, name);
  await expect(asked).toContainText("Run 0e5a7c13");
  await expect(asked).not.toContainText("Stays done in other systems");
  await field(page, name, "Reason").fill("It can't be undone");
  await field(page, name, "Type 0e5a7c13 to confirm").fill("0e5a7c13");
  await confirm(page, name).click();
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0].body.kind).toBe("terminate");
});

test("a decision on a settle timeout reads Wait again, with when it opened and its workflow read again", async ({
  page,
}) => {
  const platform = await opened(page, caller);
  const sent = await decisions(page, platform);
  const read: Route[] = [];
  await page.route(`${environment}/incidents/${ids.caller}`, (route) => {
    read.push(route);
  });
  const name = "Wait again";
  await offer(page, name).click();
  await expect(dialog(page, name)).toContainText(
    "Waits again for the steps still running, up to the same limit.",
  );
  await expect(dialog(page, name)).toContainText(
    "Transaction move in run 6d18b3f0",
  );
  await expect
    .poll(() => described(page, name))
    .toBe("To wait again, fill in Reason.");
  await field(page, name, "Reason").fill("The called run is moving");
  await page.screenshot({ path: `${captures}/wait-again-1440.png` });
  await confirm(page, name).click();
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0].body.kind).toBe("retry_safe");
  await expect(drawer(page).locator("dt:text-is('Decision') + dd")).toHaveText(
    name,
  );
  await expect.poll(() => read.length).toBe(1);
  // The answer says neither: what doesn't change stays while it is read.
  const facts = drawer(page).locator("dl.txn-facts dd");
  await expect(facts.nth(0)).toHaveText("18 min ago");
  await expect(facts.nth(1)).toHaveText("move-between-systems 3.0.0");
  const after = platform.items.find(
    (item) => item.id === ids.caller,
  ) as IncidentView;
  await read[0].fulfill({
    json: {
      ...after,
      opened_at: "2026-11-22T10:24:00Z",
      workflow: { name: "move-between-systems", version: "3.1.0" },
      run_status: "waiting",
    },
  });
  await expect(facts.nth(0)).toHaveText("30 min ago");
  await expect(facts.nth(1)).toHaveText("move-between-systems 3.1.0");
  await expect(facts.nth(4)).toContainText("Waiting");
});

test("an incident with no transaction is read again after its decision too", async ({
  page,
}) => {
  const platform = await opened(page, others[0]);
  await decisions(page, platform);
  await drawer(page).getByRole("button", { name: "Resolve incident" }).click();
  const asked = dialog(page, "Resolve incident");
  await asked.getByLabel(/End the run/).check();
  await asked.getByLabel("Reason").fill("Sent by hand");
  await asked
    .getByRole("button", { name: "Resolve incident", exact: true })
    .click();
  await expect(drawer(page).locator("dt:text-is('Decision') + dd")).toHaveText(
    "End the run",
  );
  await expect.poll(() => platform.reads).toEqual([others[0].id]);
  await expect(drawer(page).locator("dt:text-is('Opened') + dd")).toHaveText(
    "3 min ago",
  );
  await expect(drawer(page).locator("dt:text-is('Workflow') + dd")).toHaveText(
    "notify-on-change 4.0.0",
  );
});

test("the read after a decision never opens over an incident opened meanwhile, nor brings an older one back", async ({
  page,
}) => {
  const platform = await opened(page, called, {
    items: [called, unknown, ...others],
  });
  await decisions(page, platform);
  const reads: Route[] = [];
  await page.route(`${environment}/incidents/${ids.called}`, (route) => {
    reads.push(route);
  });
  const retry = async () => {
    await offer(page, "Retry the undo").click();
    await field(page, "Retry the undo", "Reason").fill("Back");
    await confirm(page, "Retry the undo").click();
  };
  await retry();
  await expect.poll(() => reads.length).toBe(1);
  await page
    .getByRole("button", {
      name: "Open incident WV-TXN-PIVOT-UNKNOWN at submit",
    })
    .click();
  await reads[0].fulfill({
    json: { ...called, status: "resolved", revision: 2 },
  });
  await drawn(page);
  await expect(title(page)).toHaveText("Incident WV-TXN-PIVOT-UNKNOWN");
  // A read answered from before the decision doesn't take it back.
  change(platform, called);
  await page.clock.fastForward(16_000);
  await page
    .getByRole("button", { name: "Open incident WV-TXN-UNDO-STUCK at add" })
    .click();
  await retry();
  await expect.poll(() => reads.length).toBe(2);
  await reads[1].fulfill({ json: called });
  await drawn(page);
  await expect(drawer(page).locator("dt:text-is('Decision') + dd")).toHaveText(
    "Retry the undo",
  );
  await expect(offers(page)).toHaveCount(0);
});

// -------------------------------------------------------------- refusals

test("a refused record shows under the result, and everything typed stays", async ({
  page,
}) => {
  const platform = await opened(page, called);
  const sent = await decisions(page, platform, (route) =>
    refuse(
      route,
      422,
      "WV-RUNTIME-OUTPUT",
      "The reconciliation record doesn't fit the transaction",
    ),
  );
  const name = "Mark as reconciled";
  await offer(page, name).click();
  await ending(page, name, "Finished").check();
  const asked = dialog(page, name);
  await asked.getByRole("checkbox").check();
  const result = field(page, name, "Result of the transaction (JSON)");
  await result.fill('{"quantity": "40"}');
  await field(page, name, "Reason").fill("The 40 were added");
  await field(page, name, "Evidence reference").fill("Log line 90412");
  await confirm(page, name).click();
  await expect(result).toHaveAttribute("aria-invalid", "true");
  const error = asked.locator("#decide-result-error");
  await expect(error).toHaveText(
    "The reconciliation record doesn't fit the transaction (WV-RUNTIME-OUTPUT)",
  );
  await expect(result).toHaveAttribute(
    "aria-describedby",
    /(^| )decide-result-error( |$)/,
  );
  await expect(result).toBeFocused();
  await expect(asked.locator(".notice")).toHaveCount(0);
  await page.screenshot({ path: `${captures}/reconcile-refused-1440.png` });
  // The result, what is wrong with it and the confirm are in view together.
  for (const part of [result, error, confirm(page, name)])
    await expect(part).toBeInViewport({ ratio: 1 });
  await expect(result).toHaveValue('{"quantity": "40"}');
  await expect(ending(page, name, "Finished")).toBeChecked();
  await expect(asked.getByRole("checkbox")).toBeChecked();
  await expect(field(page, name, "Reason")).toHaveValue("The 40 were added");
  await expect(field(page, name, "Evidence reference")).toHaveValue(
    "Log line 90412",
  );
  // Correcting it takes the refusal away; nothing is said about it twice.
  await result.fill('{"quantity": 40}');
  await expect(result).not.toHaveAttribute("aria-invalid");
  await expect(error).toHaveCount(0);
  await expect(asked.locator(".notice")).toHaveCount(0);
  await confirm(page, name).click();
  await expect.poll(() => sent.length).toBe(2);
  // A refusal is final for its receipt.
  expect(sent[1].body.receipt_id).not.toBe(sent[0].body.receipt_id);
});

for (const [why, as, message] of [
  ["that names a step", "Finished", "Nothing says how add ended"],
  [
    "of one that was undone",
    "Undone",
    "The reconciliation record doesn't fit the transaction",
  ],
] as const)
  test(`a refused record ${why} shows under the checklist`, async ({
    page,
  }) => {
    const platform = await opened(page, called);
    await decisions(page, platform, (route) =>
      refuse(route, 422, "WV-RUNTIME-OUTPUT", message),
    );
    const name = "Mark as reconciled";
    await offer(page, name).click();
    await ending(page, name, as).check();
    const asked = dialog(page, name);
    await asked.getByRole("checkbox").check();
    if (as === "Finished")
      await field(page, name, "Result of the transaction (JSON)").fill("{}");
    await field(page, name, "Reason").fill("Checked");
    await field(page, name, "Evidence reference").fill("OPS-2304");
    await confirm(page, name).click();
    const list = asked.getByRole("group", {
      name: "Check each step in its system",
    });
    await expect(list).toContainText(`${message} (WV-RUNTIME-OUTPUT)`);
    await expect(list).toHaveAttribute("aria-invalid", "true");
    if (as === "Finished")
      await expect(
        field(page, name, "Result of the transaction (JSON)"),
      ).not.toHaveAttribute("aria-invalid");
    await expect(asked.getByRole("checkbox")).toBeChecked();
    // It was about that ending: another one hasn't been refused.
    await ending(page, name, as === "Undone" ? "Finished" : "Undone").check();
    await expect(asked).not.toContainText("WV-RUNTIME-OUTPUT");
  });

test("a refused result of the step shows under it", async ({ page }) => {
  const platform = await opened(page, unknown);
  await decisions(page, platform, (route) =>
    refuse(
      route,
      422,
      "WV-RUNTIME-OUTPUT",
      "The step's output violates its schema",
    ),
  );
  const name = "It happened";
  await offer(page, name).click();
  const result = field(page, name, "Result of the step (JSON)");
  await result.fill('{"order": 1}');
  await field(page, name, "Reason").fill("Found");
  await field(page, name, "Evidence reference").fill("Search");
  await confirm(page, name).click();
  await expect(result).toHaveAttribute("aria-invalid", "true");
  await expect(dialog(page, name)).toContainText(
    "The step's output violates its schema (WV-RUNTIME-OUTPUT)",
  );
});

test("a step that is running again says to wait", async ({ page }) => {
  const platform = await opened(page, unknown);
  await decisions(page, platform, (route) =>
    refuse(
      route,
      409,
      "WV-RUNTIME-STATE",
      "The latest attempt is still running",
    ),
  );
  const name = "It didn't happen";
  await offer(page, name).click();
  await field(page, name, "Reason").fill("Not found");
  await field(page, name, "Evidence reference").fill("Search");
  await confirm(page, name).click();
  const alert = dialog(page, name).getByRole("alert");
  await expect(alert).toContainText(
    "The step is running again. Wait for it to finish, then decide.",
  );
  await expect(alert).toContainText("Support code: WV-RUNTIME-STATE");
  await expect(confirm(page, name)).not.toHaveAttribute("aria-disabled");
  await page.screenshot({ path: `${captures}/running-again-1440.png` });
});

test("a person refused the decision reads the access it needs", async ({
  page,
}) => {
  const platform = await opened(page, called);
  await decisions(page, platform, (route) =>
    refuse(route, 403, "WV-FORBIDDEN", "Not allowed"),
  );
  await undone(page);
  await confirm(page, "Mark as reconciled").click();
  const alert = dialog(page, "Mark as reconciled").getByRole("alert");
  await expect(alert).toContainText(
    "You need incident.resolve and transaction.reconcile in this environment. Ask an administrator in Settings › People and access.",
  );
  await expect(alert).toContainText("Support code: WV-FORBIDDEN");
});

/** Refuses the first decision for its revision; the platform holds a newer one. */
async function changedMeanwhile(page: Page, incident: IncidentView) {
  const platform = await opened(page, incident);
  let refusals = 0;
  const sent = await decisions(page, platform, async (route) => {
    if (refusals++) return false;
    return refuse(
      route,
      409,
      "WV-INCIDENT-REVISION",
      "Incident revision changed",
    );
  });
  return { platform, sent };
}

test("an incident that changed must be refreshed: the checklist is built again, unticked, and the rest stays", async ({
  page,
}) => {
  const { platform, sent } = await changedMeanwhile(page, called);
  await undone(page);
  const name = "Mark as reconciled";
  const asked = dialog(page, name);
  change(platform, triedAgain(called, 4));
  await confirm(page, name).click();
  const alert = asked.getByRole("alert");
  await expect(alert).toContainText(
    "Someone changed this incident since you opened it. Refresh it, check it again, then decide.",
  );
  await expect(alert).toContainText("Support code: WV-INCIDENT-REVISION");
  await expect(confirm(page, name)).toHaveAttribute("aria-disabled", "true");
  await expect
    .poll(() => described(page, name))
    .toBe("To mark it as reconciled, refresh the incident.");
  await expect(asked).not.toContainText("rebuilt the checklist");
  await page.screenshot({ path: `${captures}/reconcile-changed-1440.png` });
  // Nothing sends it, not even Enter.
  await field(page, name, "Evidence reference").press("Enter");
  await confirm(page, name).evaluate((button: HTMLElement) => button.click());
  expect(sent).toHaveLength(1);
  const reads: Route[] = [];
  await page.route(`${environment}/incidents/${ids.called}`, (route) => {
    reads.push(route);
  });
  const refresh = asked.getByRole("button", { name: "Refresh incident" });
  await refresh.focus();
  await refresh.evaluate((button: HTMLElement) => {
    button.click();
    button.click();
  });
  await expect.poll(() => reads.length).toBe(1);
  await expect(refresh).toHaveAttribute("aria-busy", "true");
  await expect(refresh).toBeFocused();
  await expect
    .poll(() => described(page, name))
    .toBe("To mark it as reconciled, refresh the incident.");
  // It couldn't be read: the old record still can't be decided on.
  await refuse(reads[0], 503, "WV-UNAVAILABLE", "Unavailable");
  await expect(alert).toContainText("Support code: WV-UNAVAILABLE");
  await expect(confirm(page, name)).toHaveAttribute("aria-disabled", "true");
  await refresh.click();
  await expect.poll(() => reads.length).toBe(2);
  await reads[1].fulfill({ json: triedAgain(called, 4) });
  await expect(alert).toHaveCount(0);
  await expect(asked).toContainText(
    "Studio read the incident again and rebuilt the checklist. Tick each step again; your reason and evidence are kept.",
  );
  await expect(asked).toContainText("Undo failed after 4 tries");
  await expect(asked.getByRole("checkbox")).not.toBeChecked();
  await expect(ending(page, name, "Undone")).toBeChecked();
  await expect(field(page, name, "Reason")).toHaveValue("The quantity is back");
  await expect(field(page, name, "Evidence reference")).toHaveValue("OPS-2304");
  await expect
    .poll(() => described(page, name))
    .toBe("To mark it as reconciled, tick the line for add.");
  await page.screenshot({ path: `${captures}/reconcile-refreshed-1440.png` });
  await asked.getByRole("checkbox").check();
  await confirm(page, name).click();
  await expect.poll(() => sent.length).toBe(2);
  expect(sent[1].revision).toBe('"2"');
});

test("the result typed for a finished transaction stays through a refresh", async ({
  page,
}) => {
  const { platform } = await changedMeanwhile(page, called);
  const name = "Mark as reconciled";
  await offer(page, name).click();
  await ending(page, name, "Finished").check();
  const asked = dialog(page, name);
  await asked.getByRole("checkbox").check();
  const result = field(page, name, "Result of the transaction (JSON)");
  await result.fill('{"quantity": 40}');
  await field(page, name, "Reason").fill("Added");
  await field(page, name, "Evidence reference").fill("Log line 90412");
  change(platform, triedAgain(called, 4));
  await confirm(page, name).click();
  await asked.getByRole("button", { name: "Refresh incident" }).click();
  await expect(asked).toContainText("Undo failed after 4 tries");
  await expect(ending(page, name, "Finished")).toBeChecked();
  await expect(result).toHaveValue('{"quantity": 40}');
});

test("Force end refused for its revision rebuilds what stays done once refreshed", async ({
  page,
}) => {
  const { platform, sent } = await changedMeanwhile(page, unknown);
  const name = "Force end";
  await offer(page, name).click();
  const asked = dialog(page, name);
  await field(page, name, "Reason").fill("Closed by hand");
  await field(page, name, "Type 7b3e9d10 to confirm").fill("7b3e9d10");
  await expect(asked.getByRole("listitem")).toHaveCount(3);
  change(platform, {
    ...unknown,
    revision: 2,
    transaction: {
      ...unknown.transaction!,
      participants: unknown.transaction!.participants.slice(1),
    },
  });
  await confirm(page, name).click();
  await expect(confirm(page, name)).toHaveAttribute("aria-disabled", "true");
  await expect
    .poll(() => described(page, name))
    .toBe("To force end it, refresh the incident.");
  await asked.getByRole("button", { name: "Refresh incident" }).click();
  await expect(asked.getByRole("listitem")).toHaveCount(2);
  await expect(asked).not.toContainText("rebuilt the checklist");
  await confirm(page, name).click();
  await expect.poll(() => sent.length).toBe(2);
  expect(sent[1].revision).toBe('"2"');
});

test("an incident decided meanwhile leaves the dialog on refresh, and says so", async ({
  page,
}) => {
  const { platform } = await changedMeanwhile(page, called);
  await undone(page);
  await confirm(page, "Mark as reconciled").click();
  change(platform, {
    ...called,
    status: "resolved",
    revision: 2,
    resolved_at: "2026-11-22T10:53:00Z",
    resolution: {
      receipt_id: "93333333-3333-4333-8333-333333333333",
      kind: "terminate",
      reason: "Closed by hand",
    },
  });
  await dialog(page, "Mark as reconciled")
    .getByRole("button", { name: "Refresh incident" })
    .click();
  await expect(dialog(page, "Mark as reconciled")).toHaveCount(0);
  await expect(
    page.getByText("Incident WV-TXN-UNDO-STUCK at add is no longer active."),
  ).toBeVisible();
  await expect(drawer(page).locator("dt:text-is('Decision') + dd")).toHaveText(
    "Force end",
  );
  await expect(title(page)).toBeFocused();
});

// ------------------------------------------------------ sent once, or again

test("a lost answer is sent again with the same receipt, and a changed decision with its own", async ({
  page,
}) => {
  const platform = await opened(page, called);
  let losing = 2;
  const sent = await decisions(page, platform, async (route) => {
    if (!losing--) return false;
    return refuse(route, 503, "WV-UNAVAILABLE", "Unavailable");
  });
  await undone(page);
  const name = "Mark as reconciled";
  await confirm(page, name).click();
  const alert = dialog(page, name).getByRole("alert");
  await expect(alert).toContainText(
    "Studio didn't get an answer, so the decision may have been recorded. Send it again unchanged: the same receipt makes that safe.",
  );
  await page.screenshot({ path: `${captures}/lost-answer-1440.png` });
  await confirm(page, name).click();
  await expect.poll(() => sent.length).toBe(2);
  expect(sent[1].body).toEqual(sent[0].body);
  await expect(alert).toBeVisible();
  await field(page, name, "Reason").fill("Another reason");
  await confirm(page, name).click();
  await expect.poll(() => sent.length).toBe(3);
  expect(sent[2].body.receipt_id).not.toBe(sent[0].body.receipt_id);
});

test("a double click sends once, and Resolving… ignores the rest", async ({
  page,
}) => {
  const platform = await opened(page, called);
  const held: Route[] = [];
  const sent = await decisions(page, platform, (route) => {
    held.push(route);
  });
  await undone(page);
  const name = "Mark as reconciled";
  await confirm(page, name).focus();
  await confirm(page, name).evaluate((button: HTMLElement) => {
    button.click();
    button.click();
  });
  const sending = dialog(page, name).getByRole("button", {
    name: "Resolving…",
  });
  await expect(sending).toBeVisible();
  await expect(sending).toBeFocused();
  await page.screenshot({ path: `${captures}/resolving-1440.png` });
  await sending.evaluate((button: HTMLElement) => button.click());
  await field(page, name, "Evidence reference").press("Enter");
  expect(sent).toHaveLength(1);
  await resolveIncident(held[0], platform, sent[0]);
  await expect(dialog(page, name)).toHaveCount(0);
  expect(sent).toHaveLength(1);
});

test("a refusal of a decision whose dialog was closed is a notice, never in another decision's dialog", async ({
  page,
}) => {
  const platform = await opened(page, called);
  const held: Route[] = [];
  const sent = await decisions(page, platform, (route) => {
    held.push(route);
  });
  await undone(page);
  await confirm(page, "Mark as reconciled").click();
  await expect.poll(() => held.length).toBe(1);
  await page.keyboard.press("Escape");
  await offer(page, "Force end").click();
  const name = "Force end";
  // One decision on an incident at a time.
  await expect(
    dialog(page, name).getByRole("button", { name: "Resolving…" }),
  ).toBeVisible();
  await refuse(
    held[0],
    409,
    "WV-INCIDENT-REVISION",
    "Incident revision changed",
  );
  await expect(
    page.getByText(
      "Incident WV-TXN-UNDO-STUCK at add was not resolved. Incident revision changed",
    ),
  ).toBeVisible();
  await expect(dialog(page, name).getByRole("alert")).toHaveCount(0);
  await expect
    .poll(() => described(page, name))
    .toBe("To force end it, fill in Reason and type the run ID.");
  expect(sent).toHaveLength(1);
});

test("an answer after the person opened another incident changes only the list", async ({
  page,
}) => {
  const platform = await opened(page, called, {
    items: [called, unknown, ...others],
  });
  const held: Route[] = [];
  const sent = await decisions(page, platform, (route) => {
    held.push(route);
  });
  await offer(page, "Retry the undo").click();
  await field(page, "Retry the undo", "Reason").fill("Back");
  await confirm(page, "Retry the undo").click();
  await expect.poll(() => held.length).toBe(1);
  await page.keyboard.press("Escape");
  await page
    .getByRole("button", {
      name: "Open incident WV-TXN-PIVOT-UNKNOWN at submit",
    })
    .click();
  await offer(page, "Force end").click();
  await expect(dialog(page, "Force end")).toBeVisible();
  const reads = platform.reads.length;
  await resolveIncident(held[0], platform, sent[0]);
  await expect(
    page.getByText("Incident WV-TXN-UNDO-STUCK at add is resolved."),
  ).toBeVisible();
  await expect(
    page.locator(`.incident-list [data-incident="${ids.called}"]`),
  ).toHaveCount(0);
  await expect(dialog(page, "Force end")).toBeVisible();
  await expect(title(page)).toHaveText("Incident WV-TXN-PIVOT-UNKNOWN");
  expect(platform.reads).toHaveLength(reads);
});

// ------------------------------------------------------- focus and widths

for (const [incident, name] of [
  [called, "Retry the undo"],
  [called, "Mark as reconciled"],
  [unknown, "It didn't happen"],
  [unknown, "Force end"],
] as const)
  test(`closing ${name} returns the focus to its offer`, async ({ page }) => {
    await opened(page, incident);
    await offer(page, name).focus();
    await page.keyboard.press("Enter");
    await expect(
      dialog(page, name).getByRole("heading", { name }),
    ).toBeFocused();
    // Tab reaches the close button, the form and the confirm, which says what it waits for.
    await page.keyboard.press("Escape");
    await expect(dialog(page, name)).toHaveCount(0);
    await expect(offer(page, name)).toBeFocused();
    await page.keyboard.press("Enter");
    await dialog(page, name)
      .getByRole("button", { name: "Cancel", exact: true })
      .click();
    await expect(offer(page, name)).toBeFocused();
  });

test("a press that doesn't move the focus, as in a desktop window, still returns it to the offer", async ({
  page,
}) => {
  await opened(page, called);
  await page.getByLabel("Workflow", { exact: true }).focus();
  await offer(page, "Retry the undo").evaluate((button: HTMLElement) =>
    button.click(),
  );
  await expect(dialog(page, "Retry the undo")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(offer(page, "Retry the undo")).toBeFocused();
});

test("a decision is made from the drawer's sheet, over the list", async ({
  page,
}) => {
  await page.setViewportSize({ width: 800, height: 900 });
  const platform = await incidentPlatform(page, {
    capabilities: reconciler,
    items: [called, unknown, ...others],
  });
  await decisions(page, platform);
  await page.goto("/operate/incidents");
  const row = (name: string) =>
    page.getByRole("button", { name: `Open incident ${name}`, exact: true });
  await row("WV-TXN-UNDO-STUCK at add").focus();
  await page.keyboard.press("Enter");
  await undone(page);
  await confirm(page, "Mark as reconciled").click();
  await expect(drawer(page).locator("dt:text-is('Decision') + dd")).toHaveText(
    "Mark as reconciled as undone",
  );
  await expect(title(page)).toBeFocused();
  // Its row left the list: the sheet returns to the next one.
  await page.keyboard.press("Escape");
  await expect(row("WV-TXN-PIVOT-UNKNOWN at submit")).toBeFocused();
});

for (const size of [
  { width: 1440, height: 900, touch: false },
  { width: 768, height: 1024, touch: false },
  { width: 600, height: 500, touch: false },
  { width: 360, height: 640, touch: true },
])
  test.describe(`at ${size.width}x${size.height}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });

    test("Mark as reconciled fits its dialog: the whole operation key wraps, its title and footer stay in view", async ({
      page,
    }) => {
      await opened(page, called);
      const name = "Mark as reconciled";
      await offer(page, name).click();
      await ending(page, name, "Finished").check();
      const asked = dialog(page, name);
      const key = asked.locator(".tick code");
      await expect(key).toHaveText(`${runs.called}:add#undo`);
      const panel = (await asked.boundingBox())!;
      // 520 px wide, or the screen's width less 8 px each side on a phone.
      expect(panel.width).toBeCloseTo(Math.min(520, size.width - 16), 0);
      const box = (await key.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(panel.x);
      expect(box.x + box.width).toBeLessThanOrEqual(panel.x + panel.width);
      for (const element of [asked, page.locator("html")])
        expect(
          await element.evaluate((node) => node.scrollWidth - node.clientWidth),
        ).toBeLessThanOrEqual(0);
      const target = size.width < 768 || size.touch ? 44 : 32;
      for (const row of await asked.locator(".tick").all())
        expect((await row.boundingBox())!.height).toBeGreaterThanOrEqual(
          target - 0.5,
        );
      const buttons = [
        asked.getByRole("button", { name: "Cancel", exact: true }),
        confirm(page, name),
      ];
      for (const button of buttons) {
        await expect(button).toBeInViewport({ ratio: 1 });
        expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(
          (size.width < 768 || size.touch ? 44 : 36) - 0.5,
        );
      }
      // The footer's buttons share their row, on a phone too.
      const [cancelBox, confirmBox] = await Promise.all(
        buttons.map((button) => button.boundingBox()),
      );
      expect(cancelBox!.y).toBeCloseTo(confirmBox!.y, 0);
      // Why the confirm waits has its own line above them.
      const reason = (await asked.locator(".confirm-reason").boundingBox())!;
      expect(reason.y + reason.height).toBeLessThanOrEqual(cancelBox!.y);
      await page.screenshot({
        path: `${captures}/reconcile-finished-${size.width}.png`,
      });
      await asked.getByLabel("Evidence reference").scrollIntoViewIfNeeded();
      await page.screenshot({
        path: `${captures}/reconcile-finished-end-${size.width}.png`,
      });
    });

    test("the offers fit the drawer, each a full target", async ({ page }) => {
      await opened(page, called, { capabilities: operator });
      await offers(page).scrollIntoViewIfNeeded();
      const region = (await offers(page).boundingBox())!;
      for (const button of await offers(page).getByRole("button").all()) {
        const box = (await button.boundingBox())!;
        expect(box.height).toBeGreaterThanOrEqual(
          (size.width < 768 || size.touch ? 44 : 36) - 0.5,
        );
        expect(box.x).toBeCloseTo(region.x, 0);
        expect(box.x + box.width).toBeLessThanOrEqual(region.x + region.width);
      }
      expect(
        await page
          .locator(".record-detail")
          .evaluate((element) => element.scrollWidth - element.clientWidth),
      ).toBeLessThanOrEqual(0);
      await page.screenshot({
        path: `${captures}/offers-${size.width}.png`,
      });
    });
  });
