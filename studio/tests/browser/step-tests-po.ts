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
// The platform's step tests for Execute in local: each POST answers the next
// scripted start (202 with the step test, or a refusal), each read the next
// scripted view of it, and Stop the canceled view. Drafts save with a growing
// revision. Every request is recorded with its body and headers.
import { expect, type Page, type Request } from "@playwright/test";
import type { StepDetailsPage } from "./step-details-po";

export const TEST_ID = "3f2a9c1e-5b6d-4e7f-8a9b-0c1d2e3f4a5b";
export const RUN_ID = "3f2a7d41-0c1d-4e2f-9a3b-4c5d6e7f8a9b";

export function view(over: Record<string, unknown> = {}) {
  return {
    id: TEST_ID,
    run_id: RUN_ID,
    status: "running",
    side_effect: "read_only",
    pending: [],
    output: null,
    error: null,
    attempts: 1,
    started_at: "2026-11-17T10:00:00Z",
    ended_at: null,
    duration_ms: null,
    ...over,
  };
}
export const succeeded = (
  output: unknown,
  over: Record<string, unknown> = {},
) =>
  view({
    status: "succeeded",
    output,
    ended_at: "2026-11-17T10:00:02Z",
    duration_ms: 2000,
    ...over,
  });
export const accepted = {
  id: TEST_ID,
  run_id: RUN_ID,
  status: "running",
  side_effect: "read_only",
  expires_at: "2026-11-17T10:05:00Z",
};
export const refusal = (
  status: number,
  code: string,
  result?: unknown,
  headers: Record<string, string> = {},
) => ({
  status,
  json: { code, message: `${code}.`, ...(result ? { result } : {}) },
  headers,
});

export interface StepTestScript {
  /** What each POST answers in turn; the last one repeats. */
  starts: { status: number; json: unknown; headers?: Record<string, string> }[];
  /** What each read answers in turn; the last one repeats. */
  views: unknown[];
  /** Hold each read until this resolves (the test runs while the spec looks). */
  holdReads?: Promise<void>;
}
export interface StepTestRecorder {
  posts: Request[];
  reads: number;
  cancels: number;
  drafts: Request[];
  /** Studio documents saved with the draft (test data, canvas). */
  documents: Request[];
}

export async function routeStepTests(
  page: Page,
  script: StepTestScript,
): Promise<StepTestRecorder> {
  const recorder: StepTestRecorder = {
    posts: [],
    reads: 0,
    cancels: 0,
    drafts: [],
    documents: [],
  };
  let revision = 0;
  // Drafts save with a growing revision and read back as saved (a reload restores them).
  const drafts = new Map<string, Record<string, unknown>>();
  await page.route("**/projects/*/drafts/*", async (route) => {
    const request = route.request();
    const id = request.url().split("/drafts/")[1].split(/[?/]/)[0];
    if (request.method() === "GET") {
      const kept = drafts.get(id);
      return kept ? route.fulfill({ json: kept }) : route.fallback();
    }
    if (request.method() !== "PUT") return route.fallback();
    recorder.drafts.push(request);
    const body = request.postDataJSON() as { document: unknown };
    const saved = { id, revision: ++revision, document: body.document };
    drafts.set(id, saved);
    await route.fulfill({ json: saved });
  });
  // The draft's studio documents (test data, canvas): none at first, then what was saved.
  const documents = new Map<string, Record<string, unknown>>();
  await page.route("**/projects/*/drafts/*/studio/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const [, draftId, , kind] = url.pathname.split("/drafts/")[1]
      ? ["", ...url.pathname.split("/drafts/")[1].split("/")]
      : [];
    const key = `${draftId}/${kind}`;
    if (request.method() === "PUT") {
      recorder.documents.push(request);
      const body = request.postDataJSON() as {
        draft_revision: number;
        document: Record<string, unknown>;
      };
      const saved = {
        draft_id: draftId,
        kind,
        revision: ((documents.get(key)?.["revision"] as number) ?? 0) + 1,
        draft_revision: body.draft_revision,
        document: body.document,
        contains_run_data: JSON.stringify(body.document).includes(
          '"test-call"',
        ),
        source_environment_ids: [],
        updated_at: "2026-11-17T10:00:00Z",
      };
      documents.set(key, saved);
      return route.fulfill({ json: saved });
    }
    const kept = documents.get(key);
    return kept
      ? route.fulfill({ json: kept })
      : route.fulfill({
          status: 404,
          json: { code: "WV-NOT-FOUND", message: "Not found" },
        });
  });
  await page.route("**/environments/*/step-tests", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    recorder.posts.push(route.request());
    const answer =
      script.starts[
        Math.min(recorder.posts.length - 1, script.starts.length - 1)
      ];
    await route.fulfill({
      status: answer.status,
      json: answer.json,
      headers: answer.headers,
    });
  });
  await page.route(`**/environments/*/step-tests/${TEST_ID}`, async (route) => {
    const index = recorder.reads++;
    if (script.holdReads) await script.holdReads;
    await route
      .fulfill({ json: script.views[Math.min(index, script.views.length - 1)] })
      .catch(() => undefined);
  });
  await page.route(
    `**/environments/*/step-tests/${TEST_ID}/cancel`,
    async (route) => {
      recorder.cancels++;
      await route.fulfill({
        json: view({ status: "cancelled", ended_at: "2026-11-17T10:00:03Z" }),
      });
    },
  );
  return recorder;
}

/** Opens the Execute step menu and returns the Execute in <environment> item. */
export async function executeIn(details: StepDetailsPage, environment: string) {
  await details.dialog
    .getByRole("button", { name: "More ways to execute" })
    .click();
  return details.page.getByRole("menuitem", {
    name: new RegExp(`^Execute in ${environment}`),
  });
}

/** The Output pane of the open step details. */
export const output = (details: StepDetailsPage) =>
  details.dialog.locator("weave-output-pane");

/** Waits until Studio has sent `count` step test starts. */
export async function sent(recorder: StepTestRecorder, count: number) {
  await expect.poll(() => recorder.posts.length).toBe(count);
}

/** The Output pane, brought into view where it is a tab of its own (narrow windows). */
export async function showOutput(details: StepDetailsPage) {
  const tab = details.dialog.getByRole("tab", { name: "Output" });
  if (await tab.isVisible()) await tab.click();
  return output(details);
}
