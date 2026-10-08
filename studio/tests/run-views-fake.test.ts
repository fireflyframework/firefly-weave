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
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import {
  childRunsQuery,
  isUnavailableRun,
  listRunLogs,
  listRunSteps,
  listRunSummaries,
  stepOutput,
  type RunSummary,
  type RunSummaryPage,
} from "../src/app/operate/run-contracts";
import {
  answerRunViews,
  fakeEnvironment,
  fakeRunViewsApi,
} from "../src/app/operate/run-views-fake";
import {
  fixtureIds,
  runViewsFixture,
} from "../src/app/operate/run-views.fixture";
import { pythonAvailable, validateWithPython } from "./run-views-oracle";

const api = fakeRunViewsApi();
const ids = (page: RunSummaryPage) => page.items.map((item) => item.id);
const summary = (page: RunSummaryPage, id: string) =>
  page.items.find((item) => item.id === id) as RunSummary;

describe("run summaries", () => {
  it("serves the editor's Runs tab query newest first, test runs included", async () => {
    const page = await listRunSummaries(api, {
      workflow: "invoice-approval",
      include_test: true,
      order: "started_desc",
      limit: 50,
    });
    expect(ids(page)).toEqual([
      fixtureIds.test,
      fixtureIds.running,
      fixtureIds.retry,
      fixtureIds.failed,
      fixtureIds.succeeded,
      fixtureIds.unavailable,
    ]);
    expect(page.next_cursor).toBeNull();
    expect(summary(page, fixtureIds.test).origin).toBe("test");
    expect(summary(page, fixtureIds.retry).retried_from_run_id).toBe(
      fixtureIds.failed,
    );
    expect(summary(page, fixtureIds.failed).failed_step).toEqual({
      node_id: "send",
      instance_key: "send[3]",
      error_code: "SMTP_REJECTED",
    });
  });
  it("hides test and archived runs by default and keeps unavailable placeholders", async () => {
    const page = await listRunSummaries(api);
    expect(ids(page)).not.toContain(fixtureIds.test);
    expect(ids(page)).not.toContain(fixtureIds.testChild);
    expect(ids(page)).not.toContain(fixtureIds.archived);
    const placeholder = page.items.find(isUnavailableRun);
    expect(placeholder).toEqual({
      id: fixtureIds.unavailable,
      unavailable: true,
      omissions: [{ path: "", reason: "classification_unavailable" }],
    });
    expect(page.items.filter((item) => !isUnavailableRun(item))).toHaveLength(
      5,
    );
  });
  it("lists children, including a test run's test children, and hides them for top-level only", async () => {
    const all = await listRunSummaries(api, { include_test: true });
    const children = await listRunSummaries(
      api,
      childRunsQuery(summary(all, fixtureIds.failed)),
    );
    expect(ids(children)).toEqual([fixtureIds.child]);
    const testChildren = await listRunSummaries(
      api,
      childRunsQuery(summary(all, fixtureIds.test)),
    );
    expect(ids(testChildren)).toEqual([fixtureIds.testChild]);
    const topLevel = await listRunSummaries(api, {
      include_test: true,
      top_level_only: true,
    });
    expect(ids(topLevel)).not.toContain(fixtureIds.child);
    expect(ids(topLevel)).not.toContain(fixtureIds.testChild);
  });
  it("orders by start time ascending or by last update", async () => {
    const ascending = await listRunSummaries(api, { order: "started_asc" });
    expect(ids(ascending)[0]).toBe(fixtureIds.unavailable);
    const updated = await listRunSummaries(api, {
      order: "updated_desc",
      include_test: true,
    });
    expect(ids(updated)[0]).toBe(fixtureIds.test);
  });
  it("pages with cursors that are bound to the filters and the order", async () => {
    const first = await listRunSummaries(api, { include_test: true, limit: 3 });
    expect(first.items).toHaveLength(3);
    const second = await listRunSummaries(api, {
      include_test: true,
      limit: 3,
      cursor: first.next_cursor!,
    });
    expect(ids(second)).not.toContain(ids(first)[0]);
    for (const changed of [
      { include_test: false },
      { include_test: true, order: "started_asc" as const },
    ])
      await expect(
        listRunSummaries(api, {
          ...changed,
          limit: 3,
          cursor: first.next_cursor!,
        }),
      ).rejects.toMatchObject({ status: 422, code: "WV-VALIDATION" });
  });
  it("rejects contradictory filters with WV-FILTER and bad values with WV-VALIDATION", async () => {
    for (const query of [
      "version=1.3.0",
      "top_level_only=true&origin=call",
      `top_level_only=true&caller_run_id=${fixtureIds.failed}`,
      "origin=test",
      "started_after=2026-10-07T12:00:00Z&started_before=2026-10-07T11:00:00Z",
      "started_after=2025-01-01T00:00:00Z&started_before=2026-10-07T00:00:00Z",
    ])
      expect(answerRunViews(`/run-summaries?${query}`), query).toMatchObject({
        status: 422,
        body: { code: "WV-FILTER" },
      });
    for (const query of [
      "colour=red",
      "workflow=a&workflow=b",
      "include_test=1",
      "limit=101",
      "status=canceled",
      "started_after=2026-10-07T12:00:00+02:00",
    ])
      expect(answerRunViews(`/run-summaries?${query}`), query).toMatchObject({
        status: 422,
        body: { code: "WV-VALIDATION" },
      });
  });
  it("matches time filters sent with an offset", async () => {
    const page = await listRunSummaries(api, {
      started_after: "2026-10-07T12:00:00+02:00",
      started_before: "2026-10-07T13:00:00+02:00",
    });
    expect(ids(page)).toEqual([fixtureIds.child, fixtureIds.failed]);
  });
});

describe("run steps", () => {
  it("orders by scheduled time and returns outputs only when asked", async () => {
    const plain = await listRunSteps(api, fixtureIds.failed);
    expect(
      plain.items.map((fact) => fact.instance_key || fact.node_id),
    ).toEqual([
      "fetch",
      "notify",
      "items",
      "send[0]",
      "send[1]",
      "send[2]",
      "send[3]",
    ]);
    expect(
      plain.items.every(
        (fact) => !("output" in fact) && !("omissions" in fact),
      ),
    ).toBe(true);
    const withOutput = await listRunSteps(api, fixtureIds.failed, {
      include: "output",
    });
    const byKey = Object.fromEntries(
      withOutput.items.map((fact) => [
        fact.instance_key || fact.node_id,
        stepOutput(fact),
      ]),
    );
    expect(byKey["send[0]"]).toEqual({ kind: "value", value: null });
    expect(byKey["send[1]"]).toEqual({
      kind: "value",
      value: { message_id: "m-1" },
    });
    expect(byKey["send[2]"]).toEqual({
      kind: "omitted",
      reason: "classified_secret",
    });
    expect(byKey["send[3]"]).toEqual({ kind: "none" });
    expect(
      withOutput.items.find((fact) => fact.node_id === "notify")?.child_run_id,
    ).toBe(fixtureIds.child);
  });
  it("filters by static step ID across iterations and marks incomplete lists", async () => {
    const send = await listRunSteps(api, fixtureIds.failed, {
      step: "send",
      limit: 2,
    });
    expect(send.items.map((fact) => fact.iteration)).toEqual([[0], [1]]);
    const rest = await listRunSteps(api, fixtureIds.failed, {
      step: "send",
      limit: 2,
      cursor: send.next_cursor!,
    });
    expect(rest.items.map((fact) => fact.instance_key)).toEqual([
      "send[2]",
      "send[3]",
    ]);
    expect(rest.next_cursor).toBeNull();
    const legacy = await listRunSteps(api, fixtureIds.succeeded);
    expect(legacy.complete).toBe(false);
    expect(legacy.items.map((fact) => fact.node_id)).toEqual([
      "approve",
      "fetch",
    ]);
    expect(
      answerRunViews(`/runs/${fixtureIds.failed}/steps?step=send%5B3%5D`),
    ).toMatchObject({
      status: 422,
    });
    expect(
      answerRunViews(`/runs/${fixtureIds.failed}/steps?limit=501`),
    ).toMatchObject({
      status: 422,
    });
  });
  it("answers 404 for an unknown run through the typed API", async () => {
    const error = await listRunSteps(
      api,
      "00000000-0000-4000-8000-0000000000ff",
    ).catch((failure: unknown) => failure);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, code: "WV-NOT-FOUND" });
  });
});

describe("run logs", () => {
  it("filters by lowest level, source and static step, oldest first", async () => {
    const warnings = await listRunLogs(api, fixtureIds.failed, {
      level: "warning",
    });
    expect(warnings.items.map((entry) => entry.level)).toEqual([
      "warning",
      "error",
      "error",
    ]);
    const engine = await listRunLogs(api, fixtureIds.failed, {
      source: "engine",
    });
    expect(engine.items.map((entry) => entry.code)).toEqual([
      "started",
      "task_failed",
    ]);
    const agent = await listRunLogs(api, fixtureIds.running, {
      node_id: "support",
    });
    expect(agent.items.map((entry) => entry.instance_key)).toEqual([
      "support#1",
      "support#1",
      "support#2",
    ]);
    const usage = agent.items.filter((entry) => entry.code === "AI.USAGE");
    expect(usage.map((entry) => entry.fields)).toEqual([
      { input_tokens: 812, output_tokens: 96 },
      { input_tokens: 1004, output_tokens: 41 },
    ]);
    expect((await listRunLogs(api, fixtureIds.succeeded)).items).toEqual([]);
  });
  it("leaves other URLs to the caller", () => {
    expect(
      answerRunViews(`${fakeEnvironment}/runs/${fixtureIds.failed}`),
    ).toBeNull();
    expect(answerRunViews("/runs/not-a-run/logs")).toMatchObject({
      status: 404,
    });
  });
});

describe.skipIf(!pythonAvailable)(
  "fake answers against the Python contract",
  () => {
    it("serves only pages the Python models accept", () => {
      const body = (url: string) => {
        const answer = answerRunViews(url);
        expect(answer?.status, url).toBe(200);
        return answer!.body;
      };
      const runIds = runViewsFixture.runs.map((run) => run.summary.id);
      validateWithPython({
        summaries: [
          "/run-summaries",
          "/run-summaries?include_test=true&include_archived=true",
          "/run-summaries?include_test=true&limit=2",
          "/run-summaries?order=updated_desc&has_active_incident=true",
        ].map(body),
        steps: runIds.flatMap((id) => [
          body(`/runs/${id}/steps`),
          body(`/runs/${id}/steps?include=output&limit=2`),
        ]),
        logs: runIds.map((id) => body(`/runs/${id}/logs?level=debug`)),
      });
    });
  },
);
