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
// The run detail's commands: Cancel run, Retry run, Send signal and Export
// history, with the refusals the platform answers. The server authorizes and
// checks every command; Studio leaves out the buttons it would refuse.
import { readFileSync } from "node:fs";
import { test, expect, Locator, Page, Route } from "@playwright/test";
import { connected } from "./support";

const project = "**/studio/api/api/v1/tenants/tenant/projects/project";
const environment = `${project}/environments/development`;
const runId = "5e6f7a8b-0000-4000-8000-000000000001";
const retryId = "5e6f7a8b-0000-4000-8000-000000000002";
const otherId = "9c8d7e6f-0000-4000-8000-000000000009";
const startedId = "7a7a7a7a-0000-4000-8000-0000000000aa";
const sameVersion = "a1111111-1111-4111-8111-111111111111";
const newVersion = "a2222222-2222-4222-8222-222222222222";
const activation = (id: string, versionId: string) => ({
  id,
  name: "expense-review",
  revision: 1,
  request: { version_id: versionId },
  retired: false,
});
const workflow = (version: string) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "expense-review", version },
  spec: {
    inputSchema: {
      type: "object",
      properties: { amount: { type: "integer", title: "Amount" } },
    },
    steps: [
      {
        id: "approval",
        kind: "signal",
        name: "customer-approved",
        timeoutSeconds: 3600,
        payloadSchema: { type: "object" },
      },
    ],
  },
});
/** A second run in the list, for the tests that open another run meanwhile. */
const otherRun = (status: string): Record<string, unknown> => ({
  id: otherId,
  business_key: "order-9",
  activation: activation("act-same", sameVersion),
  artifact_digest: "e".repeat(64),
  state: {
    status,
    input: { amount: 9 },
    active: status === "waiting" ? ["approval"] : [],
  },
});

interface Start {
  /** The second run in the list, when a test opens another run. */
  other?: boolean;
  /** Where the page opens; the list lets Studio push the run's address. */
  from?: string;
}

async function runPage(
  page: Page,
  status: string,
  capabilities: string[],
  start: Start = {},
) {
  await connected(page, { capabilities: ["run.read", ...capabilities] });
  const run: Record<string, unknown> = {
    id: runId,
    business_key: "order-7",
    activation: activation("act-same", sameVersion),
    artifact_digest: "d".repeat(64),
    state: {
      status,
      input: { amount: 42 },
      active: status === "waiting" ? ["approval"] : [],
    },
  };
  const runs = [run, ...(start.other ? [otherRun(status)] : [])];
  await page.route(`${environment}/runs?*`, (r) =>
    r.fulfill({ json: { items: runs, next_cursor: null } }),
  );
  for (const item of runs) {
    await page.route(`${environment}/runs/${item["id"]}`, (r) =>
      r.fulfill({ json: item }),
    );
    await page.route(`${environment}/runs/${item["id"]}/lifecycle`, (r) =>
      r.fulfill({
        json: {
          run_id: item["id"],
          archived: false,
          purged: false,
          revision: 1,
        },
      }),
    );
    await page.route(`${environment}/runs/${item["id"]}/history?*`, (r) =>
      r.fulfill({ json: { events: [], next_cursor: null } }),
    );
  }
  for (const [id, version] of [
    [sameVersion, "1.0.0"],
    [newVersion, "1.2.0"],
  ])
    await page.route(`${project}/workflows/${id}/export`, (r) =>
      r.fulfill({ json: { document: workflow(version) } }),
    );
  await page.route(`${project}/workflows?*`, (r) =>
    r.fulfill({
      json: {
        items: [
          { id: sameVersion, name: "expense-review", version: "1.0.0" },
          { id: newVersion, name: "expense-review", version: "1.2.0" },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route(`${environment}/activations?*`, (r) =>
    r.fulfill({
      json: {
        items: [
          activation("act-same", sameVersion),
          activation("act-new", newVersion),
        ],
        next_cursor: null,
      },
    }),
  );
  await page.goto(start.from ?? `/operate/runs/${runId}`);
  const detail = page.locator(".record-detail");
  if (start.from)
    await page.locator(".resource-row", { hasText: "order-7" }).click();
  await expect(detail).toContainText("Workflow expense-review 1.0.0");
  return { run, detail };
}

test("Retry run finds later activations and uncached version metadata", async ({
  page,
}) => {
  const { detail } = await runPage(page, "failed", [
    "run.retry",
    "catalog.read",
  ]);
  const laterVersion = "a3333333-3333-4333-8333-333333333333";
  await page.route(`${environment}/activations?*`, (r) =>
    r.fulfill({
      json: new URL(r.request().url()).searchParams.has("cursor")
        ? { items: [activation("act-later", laterVersion)], next_cursor: null }
        : {
            items: [
              activation("act-same", sameVersion),
              activation("act-new", newVersion),
              ...Array.from({ length: 48 }, (_, i) => ({
                ...activation(`other-${i}`, sameVersion),
                name: `other-workflow-${i}`,
              })),
            ],
            next_cursor: "later",
          },
    }),
  );
  await page.route(`${project}/workflows?*`, (r) =>
    r.fulfill({
      json: new URL(r.request().url()).searchParams.has("cursor")
        ? {
            items: [
              { id: laterVersion, name: "expense-review", version: "1.3.0" },
            ],
            next_cursor: null,
          }
        : {
            items: [
              { id: sameVersion, name: "expense-review", version: "1.0.0" },
            ],
            next_cursor: "versions",
          },
    }),
  );
  await detail.getByRole("button", { name: "Retry run", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Retry this run" });
  await expect(
    dialog.getByLabel("Version to run").locator("option").nth(2),
  ).toHaveText("Latest active version (1.3.0)");
});

test("Retry run explains bounded activation reads without claiming latest", async ({
  page,
}) => {
  const { detail } = await runPage(page, "failed", [
    "run.retry",
    "catalog.read",
  ]);
  let reads = 0;
  await page.route(`${environment}/activations?*`, (r) => {
    reads++;
    return r.fulfill({
      json: {
        items: [activation("act-new", newVersion)],
        next_cursor: `page-${reads}`,
      },
    });
  });
  await detail.getByRole("button", { name: "Retry run", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Retry this run" });
  await expect(dialog).toContainText(
    "Studio could not check every active version",
  );
  await expect(
    dialog.getByLabel("Version to run").locator("option").nth(2),
  ).toHaveText("Newer active version (1.2.0)");
  expect(reads).toBe(10);
});

const cancelRun = async (page: Page, detail: Locator, reason = "Stop it") => {
  await detail.getByRole("button", { name: "Cancel run", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Cancel this run?" });
  await dialog.getByLabel("Reason").fill(reason);
  await dialog.getByRole("button", { name: "Cancel run", exact: true }).click();
};
const sendSignal = async (page: Page, detail: Locator) => {
  await detail.getByRole("button", { name: "Send signal" }).click();
  await page
    .getByRole("dialog", { name: "Send a signal to this run?" })
    .getByRole("button", { name: "Send signal" })
    .click();
};
const retryRun = async (page: Page, detail: Locator) => {
  await detail.getByRole("button", { name: "Retry run", exact: true }).click();
  await page
    .getByRole("dialog", { name: "Retry this run" })
    .getByRole("button", { name: "Retry run" })
    .click();
};
const denied = (r: Route) =>
  r.fulfill({ status: 403, json: { code: "WV-DENIED", message: "Denied" } });
const signalReceipt = {
  id: "b1111111-1111-4111-8111-111111111111",
  request_hash: "h",
  accepted_at: "2026-10-08T09:00:00Z",
};

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    // Where the detail is a side sheet it covers the page's banners, so a
    // notice carries what they say, and Check now.
    const covered = viewport.width <= 1024;
    const lostAnswer = (page: Page) =>
      page.locator(covered ? ".toast" : ".unknown-banner");

    test("Cancel run asks for a reason and reads the run again", async ({
      page,
    }) => {
      const { run, detail } = await runPage(page, "waiting", ["run.cancel"]);
      const bodies: unknown[] = [];
      await page.route(`${environment}/runs/${runId}/cancel`, (r) => {
        bodies.push(r.request().postDataJSON());
        run["state"] = { status: "cancelled", input: { amount: 42 } };
        return r.fulfill({ json: run });
      });
      await detail.getByRole("button", { name: "Cancel run" }).click();
      const dialog = page.getByRole("dialog", { name: "Cancel this run?" });
      await expect(dialog).toContainText(
        "Steps that already started may still finish outside Weave. Give a reason for the audit log.",
      );
      await expect(
        dialog.getByRole("button", { name: "Cancel run" }),
      ).toBeDisabled();
      await dialog.getByLabel("Reason").fill("Customer withdrew the order");
      await dialog.getByRole("button", { name: "Cancel run" }).click();
      await expect(page.getByText(/^Run canceled\./)).toBeVisible();
      expect(bodies).toEqual([{ reason: "Customer withdrew the order" }]);
      await expect(detail.locator(".run-meta .status-pill")).toHaveText(
        "Canceled",
      );
      await expect(
        detail.getByRole("button", { name: "Cancel run" }),
      ).toHaveCount(0);
    });

    test("a reason longer than 2,000 characters is explained and can't be sent", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "waiting", ["run.cancel"]);
      await detail.getByRole("button", { name: "Cancel run" }).click();
      const dialog = page.getByRole("dialog", { name: "Cancel this run?" });
      await dialog.getByLabel("Reason").fill("x".repeat(2001));
      await expect(
        dialog.getByText("Keep the reason to 2,000 characters or fewer."),
      ).toBeVisible();
      await expect(
        dialog.getByRole("button", { name: "Cancel run" }),
      ).toBeDisabled();
      await dialog.getByLabel("Reason").fill("x".repeat(2000));
      await expect(
        dialog.getByRole("button", { name: "Cancel run" }),
      ).toBeEnabled();
    });

    test("a run that finished after it was shown refuses Cancel run with WV-RUNTIME-TERMINAL", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "running", ["run.cancel"]);
      await page.route(`${environment}/runs/${runId}/cancel`, (r) =>
        r.fulfill({
          status: 409,
          json: {
            code: "WV-RUNTIME-TERMINAL",
            message: "Historical terminal runs cannot be cancelled",
          },
        }),
      );
      await detail.getByRole("button", { name: "Cancel run" }).click();
      const dialog = page.getByRole("dialog", { name: "Cancel this run?" });
      await dialog.getByLabel("Reason").fill("Stop it");
      await dialog.getByRole("button", { name: "Cancel run" }).click();
      const banner = page.locator(".error-banner");
      await expect(banner).toContainText(
        "Historical terminal runs cannot be cancelled",
      );
      await expect(banner).toContainText("Support code: WV-RUNTIME-TERMINAL");
    });

    test("Retry run offers the same or the latest version with the run's input", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "failed", [
        "run.retry",
        "catalog.read",
      ]);
      const sent: { body: Record<string, unknown>; key?: string }[] = [];
      await page.route(`${environment}/runs/${runId}/retry`, (r) => {
        sent.push({
          body: r.request().postDataJSON(),
          key: r.request().headers()["idempotency-key"],
        });
        return r.fulfill({
          status: 201,
          json: {
            id: retryId,
            state: { status: "queued" },
            parent_run_id: runId,
          },
        });
      });
      await detail.getByRole("button", { name: "Retry run" }).click();
      const dialog = page.getByRole("dialog", { name: "Retry this run" });
      await expect(dialog).toContainText(
        "Starts a new run linked to run 5e6f7a8b with the input below.",
      );
      const version = dialog.getByLabel("Version to run");
      await expect(version.locator("option:checked")).toHaveText(
        "Same version (1.0.0)",
      );
      await expect(version.locator("option").nth(2)).toHaveText(
        "Latest active version (1.2.0)",
      );
      await expect(dialog.getByLabel("Amount")).toHaveValue("42");
      await dialog.getByRole("button", { name: "Retry run" }).click();
      await expect(page.getByText("Started retry 5e6f7a8b.")).toBeVisible();
      expect(sent).toHaveLength(1);
      expect(sent[0].key).toBeTruthy();
      expect(sent[0].body).toEqual({
        activation_id: "act-same",
        input: { amount: 42 },
        business_key: "order-7",
      });
    });

    test("a run that is still going on the server refuses Retry run with WV-RUNTIME-STATE", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "failed", ["run.retry"]);
      await page.route(`${environment}/runs/${runId}/retry`, (r) =>
        r.fulfill({
          status: 409,
          json: {
            code: "WV-RUNTIME-STATE",
            message: "Only a terminal run can create a linked retry",
          },
        }),
      );
      await detail.getByRole("button", { name: "Retry run" }).click();
      const dialog = page.getByRole("dialog", { name: "Retry this run" });
      // Without catalog.read only the run's own version is offered.
      await expect(
        dialog.getByLabel("Version to run").locator("option"),
      ).toHaveText(["Choose an active version", "Same version (1.0.0)"]);
      await dialog.getByRole("button", { name: "Retry run" }).click();
      await expect(dialog.getByRole("alert")).toContainText(
        "The run didn't start. Only a terminal run can create a linked retry",
      );
      await expect(dialog.getByRole("alert")).toContainText(
        "Support code: WV-RUNTIME-STATE",
      );
    });

    test("Send signal prefills the waiting step's signal and checks the payload", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "waiting", ["run.signal"]);
      const bodies: Record<string, unknown>[] = [];
      await page.route(`${environment}/runs/${runId}/signals`, (r) => {
        bodies.push(r.request().postDataJSON());
        return r.fulfill({
          status: 202,
          json: {
            id: "b1111111-1111-4111-8111-111111111111",
            request_hash: "h",
            accepted_at: new Date().toISOString(),
          },
        });
      });
      await detail.getByRole("button", { name: "Send signal" }).click();
      const dialog = page.getByRole("dialog", {
        name: "Send a signal to this run?",
      });
      await expect(dialog.getByLabel("Signal name")).toHaveValue(
        "customer-approved",
      );
      const eventId = await dialog.getByLabel("Event ID").inputValue();
      expect(eventId).toMatch(/^[0-9a-f-]{36}$/);
      await dialog.getByLabel("Payload (JSON)").fill("{approved}");
      await expect(
        dialog.getByText(
          'Enter the payload as JSON, for example {"approved": true}.',
        ),
      ).toBeVisible();
      await dialog.getByLabel("Payload (JSON)").fill('{"approved": true}');
      await dialog.getByRole("button", { name: "Send signal" }).click();
      await expect(page.getByText("Signal sent.")).toBeVisible();
      expect(bodies).toEqual([
        { eventId, name: "customer-approved", payload: { approved: true } },
      ]);
    });

    test("Export history downloads the run's history file", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "succeeded", []);
      await page.route(`${environment}/runs/${runId}/export?limit=1000`, (r) =>
        r.fulfill({
          json: {
            version: "weave/history-v1",
            run_id: runId,
            events: [{ sequence: 1, type: "run_started" }],
            high_water_sequence: 1500,
            bounded_prefix: true,
          },
        }),
      );
      // A reader sees Export history and none of the commands.
      for (const name of ["Cancel run", "Retry run", "Send signal"])
        await expect(detail.getByRole("button", { name })).toHaveCount(0);
      const download = page.waitForEvent("download");
      await detail.getByRole("button", { name: "Export history" }).click();
      const file = await download;
      expect(file.suggestedFilename()).toBe(`run-${runId}-history.json`);
      const saved = JSON.parse(readFileSync((await file.path())!, "utf8"));
      expect(saved.run_id).toBe(runId);
      await expect(
        page.getByText(
          `Exported run-${runId}-history.json. It holds the run's first 1,000 events.`,
        ),
      ).toBeVisible();
    });

    test("Export history says nothing about a bound when the whole history fits", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "succeeded", []);
      await page.route(`${environment}/runs/${runId}/export?limit=1000`, (r) =>
        r.fulfill({
          json: {
            version: "weave/history-v1",
            run_id: runId,
            events: [{ sequence: 1, type: "run_started" }],
            high_water_sequence: 1,
            bounded_prefix: false,
          },
        }),
      );
      const download = page.waitForEvent("download");
      await detail.getByRole("button", { name: "Export history" }).click();
      await download;
      const notice = page.getByText(`Exported run-${runId}-history.json.`);
      await expect(notice).toBeVisible();
      await expect(notice).not.toContainText("first 1,000 events");
    });

    test("each command shows only when the run's state and the person's access allow it", async ({
      page,
    }) => {
      const { run, detail } = await runPage(page, "running", [
        "run.cancel",
        "run.retry",
        "run.signal",
      ]);
      const shown = async () => {
        await detail.getByRole("button", { name: "Refresh" }).click();
        const names = ["Send signal", "Cancel run", "Retry run"];
        return (
          await Promise.all(
            names.map(async (name) =>
              (await detail.getByRole("button", { name, exact: true }).count())
                ? name
                : "",
            ),
          )
        ).filter(Boolean);
      };
      // Running: a signal has nothing to wake, and a retry nothing to repeat.
      expect(await shown()).toEqual(["Cancel run"]);
      run["state"] = { status: "waiting", active: ["approval"] };
      await expect.poll(shown).toEqual(["Send signal", "Cancel run"]);
      run["state"] = { status: "timed_out", active: [] };
      await expect.poll(shown).toEqual(["Retry run"]);
    });

    test("a refused Cancel run names the access it needs and keeps the code", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "waiting", ["run.cancel"]);
      await page.route(`${environment}/runs/${runId}/cancel`, denied);
      await cancelRun(page, detail);
      const banner = page.locator(".error-banner");
      await expect(banner).toContainText(
        "You need run.cancel in this environment.",
      );
      await expect(banner).toContainText("Support code: WV-DENIED");
      if (covered)
        await expect(page.locator(".toast")).toContainText(
          "Run 5e6f7a8b was not canceled. You need run.cancel in this environment.",
        );
    });

    test("a refused Send signal names the access it needs and keeps the code", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "waiting", ["run.signal"]);
      await page.route(`${environment}/runs/${runId}/signals`, denied);
      await sendSignal(page, detail);
      const banner = page.locator(".error-banner");
      await expect(banner).toContainText(
        "You need run.signal in this environment.",
      );
      await expect(banner).toContainText("Support code: WV-DENIED");
      if (covered)
        await expect(page.locator(".toast")).toContainText(
          "Run 5e6f7a8b did not get the signal. You need run.signal in this environment.",
        );
    });

    test("a refused Retry run names the access it needs in its dialog", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "failed", ["run.retry"]);
      await page.route(`${environment}/runs/${runId}/retry`, denied);
      await retryRun(page, detail);
      const alert = page
        .getByRole("dialog", { name: "Retry this run" })
        .getByRole("alert");
      await expect(alert).toContainText(
        "You need run.retry in this environment.",
      );
      await expect(alert).toContainText("Support code: WV-DENIED");
    });

    test("a refused Export history names the access it needs and keeps the code", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "succeeded", []);
      await page.route(`${environment}/runs/${runId}/export?*`, denied);
      await detail.getByRole("button", { name: "Export history" }).click();
      const banner = page.locator(".error-banner");
      await expect(banner).toContainText(
        "You need run.read in this environment.",
      );
      await expect(banner).toContainText("Support code: WV-DENIED");
      if (covered)
        await expect(page.locator(".toast")).toContainText(
          "The history of run 5e6f7a8b was not exported.",
        );
    });

    test("a lost answer to Cancel run offers Check now and resends the same request", async ({
      page,
    }) => {
      const { run, detail } = await runPage(page, "waiting", ["run.cancel"]);
      const keys: (string | undefined)[] = [];
      await page.route(`${environment}/runs/${runId}/cancel`, (r) => {
        keys.push(r.request().headers()["idempotency-key"]);
        if (keys.length === 1) return r.abort("connectionreset");
        run["state"] = { status: "cancelled", input: { amount: 42 } };
        return r.fulfill({ json: run });
      });
      await cancelRun(page, detail);
      const unknown = lostAnswer(page);
      await expect(unknown).toContainText(
        'Studio didn\'t get an answer for "Cancel run".',
      );
      // A second Cancel run waits for Check now instead of going out blind.
      await expect(
        detail.getByRole("button", { name: "Cancel run", exact: true }),
      ).toBeDisabled();
      await unknown.getByRole("button", { name: "Check now" }).click();
      await expect(
        page.getByText("Checked. Cancel run finished."),
      ).toBeVisible();
      expect(keys).toHaveLength(2);
      expect(keys[0]).toBeTruthy();
      expect(keys[1]).toBe(keys[0]);
      // The run on screen shows what the check found.
      await expect(detail.locator(".run-meta .status-pill")).toHaveText(
        "Canceled",
      );
    });

    test("a lost answer to Retry run leaves its dialog so Check now can be used", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "failed", ["run.retry"]);
      const keys: (string | undefined)[] = [];
      await page.route(`${environment}/runs/${runId}/retry`, (r) => {
        keys.push(r.request().headers()["idempotency-key"]);
        if (keys.length === 1) return r.abort("connectionreset");
        return r.fulfill({
          status: 201,
          json: { id: retryId, state: { status: "queued" } },
        });
      });
      await retryRun(page, detail);
      await expect(
        page.getByRole("dialog", { name: "Retry this run" }),
      ).toHaveCount(0);
      const unknown = lostAnswer(page);
      await expect(unknown).toContainText(
        'Studio didn\'t get an answer for "Retry run".',
      );
      await unknown.getByRole("button", { name: "Check now" }).click();
      await expect(
        page.getByText("Checked. Retry run finished."),
      ).toBeVisible();
      expect(keys[1]).toBe(keys[0]);
    });

    test("closing a run while its Cancel run dialog is open sends nothing", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "waiting", ["run.cancel"], {
        from: "/operate/runs",
      });
      const sent: unknown[] = [];
      await page.route(`${environment}/runs/${runId}/cancel`, (r) => {
        sent.push(r.request().postDataJSON());
        return r.fulfill({ json: {} });
      });
      await detail
        .getByRole("button", { name: "Cancel run", exact: true })
        .click();
      const dialog = page.getByRole("dialog", { name: "Cancel this run?" });
      await dialog.getByLabel("Reason").fill("Stop it");
      // Back closes the run while the dialog stays up.
      await page.goBack();
      await expect(detail).toHaveCount(0);
      await dialog
        .getByRole("button", { name: "Cancel run", exact: true })
        .click();
      await expect(
        page.getByText("Run 5e6f7a8b was not canceled: it is no longer open."),
      ).toBeVisible();
      expect(sent).toEqual([]);
    });

    test("closing a run while Cancel run is on its way leaves no warning and still says what happened", async ({
      page,
    }) => {
      const warnings: string[] = [];
      page.on("console", (message) => {
        if (/NG0\d+/.test(message.text())) warnings.push(message.text());
      });
      const { run, detail } = await runPage(page, "waiting", ["run.cancel"]);
      let finish!: () => void;
      const gate = new Promise<void>((resolve) => (finish = resolve));
      await page.route(`${environment}/runs/${runId}/cancel`, async (r) => {
        await gate;
        run["state"] = { status: "cancelled", input: { amount: 42 } };
        await r.fulfill({ json: run });
      });
      await cancelRun(page, detail);
      await detail.getByRole("button", { name: "Close detail" }).click();
      await expect(detail).toHaveCount(0);
      const address = page.url();
      finish();
      await expect(page.getByText(/^Run 5e6f7a8b canceled\./)).toBeVisible();
      expect(page.url()).toBe(address);
      expect(warnings).toEqual([]);
    });

    test("a retry answered after its run was closed only says what happened", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "failed", ["run.retry"], {
        from: "/operate/runs",
      });
      let finish!: () => void;
      const gate = new Promise<void>((resolve) => (finish = resolve));
      await page.route(`${environment}/runs/${runId}/retry`, async (r) => {
        await gate;
        await r.fulfill({
          status: 201,
          json: { id: startedId, state: { status: "queued" } },
        });
      });
      await retryRun(page, detail);
      // Back closes the run, and its dialog with it, while the retry is out.
      await page.goBack();
      await expect(detail).toHaveCount(0);
      const address = page.url();
      finish();
      await expect(
        page.getByText("Started retry 7a7a7a7a of run 5e6f7a8b."),
      ).toBeVisible();
      await expect(page.getByRole("dialog")).toHaveCount(0);
      expect(page.url()).toBe(address);
    });

    test("a retry refused after its run was closed never reaches the run opened next", async ({
      page,
    }) => {
      const { detail } = await runPage(page, "failed", ["run.retry"], {
        other: true,
        from: "/operate/runs",
      });
      let finish!: () => void;
      const gate = new Promise<void>((resolve) => (finish = resolve));
      await page.route(`${environment}/runs/${runId}/retry`, async (r) => {
        await gate;
        await r.fulfill({
          status: 409,
          json: {
            code: "WV-RUNTIME-STATE",
            message: "Only a terminal run can create a linked retry",
          },
        });
      });
      await retryRun(page, detail);
      await page.goBack();
      await expect(detail).toHaveCount(0);
      await page.locator(".resource-row", { hasText: "order-9" }).click();
      await expect(detail.locator(".run-meta")).toContainText("order-9");
      finish();
      const notice = page.getByText("Run 5e6f7a8b was not retried.");
      await expect(notice).toBeVisible();
      await expect(notice).toContainText("Support code: WV-RUNTIME-STATE");
      await expect(
        page.getByRole("dialog", { name: "Retry this run" }),
      ).toHaveCount(0);
      await expect(page.locator(".error-banner")).toHaveCount(0);
      await expect(page).toHaveURL(new RegExp(`/operate/runs/${otherId}$`));
    });
  });

// A command's answer belongs to the run it was sent for, never to the run the
// person opened while it was on its way. Needs the list beside the detail.
test.describe("another run opened while a command is on its way", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  const moveOn = async (
    page: Page,
    options: {
      status: string;
      capabilities: string[];
      command: string;
      answer: (r: Route) => Promise<void>;
      send: (page: Page, detail: Locator) => Promise<void>;
    },
  ) => {
    const { detail } = await runPage(
      page,
      options.status,
      options.capabilities,
      { other: true },
    );
    let finish!: () => void;
    const gate = new Promise<void>((resolve) => (finish = resolve));
    await page.route(
      `${environment}/runs/${runId}/${options.command}`,
      async (r) => {
        await gate;
        await options.answer(r);
      },
    );
    await options.send(page, detail);
    await page.locator(".resource-row", { hasText: "order-9" }).click();
    await expect(detail.locator(".run-meta")).toContainText("order-9");
    return {
      detail,
      // Lets the first run's answer arrive and the page handle it.
      answerArrives: async () => {
        const answered = page.waitForResponse((response) =>
          response.url().endsWith(`/runs/${runId}/${options.command}`),
        );
        finish();
        await answered;
        await page.evaluate(
          () =>
            new Promise<void>((resolve) =>
              requestAnimationFrame(() => setTimeout(resolve, 100)),
            ),
        );
      },
    };
  };
  const stillTheOtherRun = async (page: Page, detail: Locator) => {
    await expect(detail.locator(".run-meta")).toContainText("order-9");
    await expect(page).toHaveURL(new RegExp(`/operate/runs/${otherId}$`));
    await expect(page.locator(".error-banner")).toHaveCount(0);
    await expect(detail.locator(".run-meta .status-pill")).toHaveText(
      "Waiting",
    );
    // Its own commands are free again once the first answer is in.
    await expect(
      detail.getByRole("button", { name: "Cancel run", exact: true }),
    ).toBeEnabled();
  };

  test("a finished Cancel run never changes the run opened meanwhile", async ({
    page,
  }) => {
    const { detail, answerArrives } = await moveOn(page, {
      status: "waiting",
      capabilities: ["run.cancel"],
      command: "cancel",
      send: (page, detail) => cancelRun(page, detail),
      answer: (r) =>
        r.fulfill({
          json: {
            id: runId,
            state: { status: "cancelled", input: { amount: 42 } },
          },
        }),
    });
    await answerArrives();
    await expect(
      page.getByText(
        "Run 5e6f7a8b canceled. Steps that already started may still finish outside Weave.",
      ),
    ).toBeVisible();
    await stillTheOtherRun(page, detail);
  });

  test("a refused Cancel run never becomes the error of the run opened meanwhile", async ({
    page,
  }) => {
    const { detail, answerArrives } = await moveOn(page, {
      status: "waiting",
      capabilities: ["run.cancel"],
      command: "cancel",
      send: (page, detail) => cancelRun(page, detail),
      answer: (r) =>
        r.fulfill({
          status: 409,
          json: {
            code: "WV-RUNTIME-TERMINAL",
            message: "Historical terminal runs cannot be cancelled",
          },
        }),
    });
    await answerArrives();
    // The person still learns what happened to the run they asked about.
    const notice = page.getByText("Run 5e6f7a8b was not canceled.");
    await expect(notice).toBeVisible();
    await expect(notice).toContainText(
      "Historical terminal runs cannot be cancelled",
    );
    await expect(notice).toContainText("Support code: WV-RUNTIME-TERMINAL");
    await stillTheOtherRun(page, detail);
  });

  test("a refused Cancel run for another run still names the missing access", async ({
    page,
  }) => {
    const { detail, answerArrives } = await moveOn(page, {
      status: "waiting",
      capabilities: ["run.cancel"],
      command: "cancel",
      send: (page, detail) => cancelRun(page, detail),
      answer: denied,
    });
    await answerArrives();
    const notice = page.getByText("Run 5e6f7a8b was not canceled.");
    await expect(notice).toContainText(
      "You need run.cancel in this environment.",
    );
    await expect(notice).toContainText("Support code: WV-DENIED");
    await stillTheOtherRun(page, detail);
  });

  test("a sent signal never changes the run opened meanwhile", async ({
    page,
  }) => {
    const { detail, answerArrives } = await moveOn(page, {
      status: "waiting",
      capabilities: ["run.signal"],
      command: "signals",
      send: sendSignal,
      answer: (r) => r.fulfill({ status: 202, json: signalReceipt }),
    });
    await answerArrives();
    await expect(page.getByText("Signal sent to run 5e6f7a8b.")).toBeVisible();
    await expect(detail.locator(".run-meta")).toContainText("order-9");
    await expect(page).toHaveURL(new RegExp(`/operate/runs/${otherId}$`));
    await expect(page.locator(".error-banner")).toHaveCount(0);
    await expect(detail.locator(".run-meta .status-pill")).toHaveText(
      "Waiting",
    );
  });

  test("a refused signal never becomes the error of the run opened meanwhile", async ({
    page,
  }) => {
    const { detail, answerArrives } = await moveOn(page, {
      status: "waiting",
      capabilities: ["run.signal"],
      command: "signals",
      send: sendSignal,
      answer: (r) =>
        r.fulfill({
          status: 409,
          json: {
            code: "WV-RUNTIME-SIGNAL",
            message: "The run is not waiting for this signal",
          },
        }),
    });
    await answerArrives();
    const notice = page.getByText("Run 5e6f7a8b did not get the signal.");
    await expect(notice).toBeVisible();
    await expect(notice).toContainText(
      "The run is not waiting for this signal",
    );
    await expect(notice).toContainText("Support code: WV-RUNTIME-SIGNAL");
    await expect(detail.locator(".run-meta")).toContainText("order-9");
    await expect(page.locator(".error-banner")).toHaveCount(0);
  });
});
