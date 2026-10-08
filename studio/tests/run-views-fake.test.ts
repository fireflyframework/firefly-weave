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
  type FakeAnswer,
} from "../src/app/operate/run-views-fake";
import {
  fixtureIds,
  runViewsFixture,
} from "../src/app/operate/run-views.fixture";
import {
  classifyQueriesWithPython,
  pythonAvailable,
  type QueryModel,
  validateWithPython,
} from "./run-views-oracle";

const api = fakeRunViewsApi();
const ids = (page: RunSummaryPage) => page.items.map((item) => item.id);
const summary = (page: RunSummaryPage, id: string) =>
  page.items.find((item) => item.id === id) as RunSummary;
// Query strings refused with WV-FILTER and with WV-VALIDATION.
const contradictoryQueries = [
  "version=1.3.0",
  "top_level_only=true&origin=call",
  `top_level_only=true&caller_run_id=${fixtureIds.failed}`,
  "origin=test",
  "started_after=2026-10-07T12:00:00Z&started_before=2026-10-07T11:00:00Z",
  "started_after=2025-01-01T00:00:00Z&started_before=2026-10-07T00:00:00Z",
];
const invalidQueries = [
  "colour=red",
  "workflow=a&workflow=b",
  "include_test=1",
  "limit=101",
  "status=canceled",
  "started_after=2026-10-07T12:00:00+02:00",
  // An offset can push the instant past year 9999 or before year 1.
  "started_after=9999-12-31T23:59:59-23:59",
  "started_before=9999-12-31T23:59:59-01:00",
  "started_after=0001-01-01T00:00:00%2B23:59",
  "started_before=0001-01-01T00:00:00%2B00:01",
];
/** The cursor's payload: base64url of the issued bound and offset. */
const decodeCursor = (cursor: string) => {
  const base64 = cursor.replace(/-/g, "+").replace(/_/g, "/");
  return atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, "="));
};

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
    for (const query of contradictoryQueries)
      expect(answerRunViews(`/run-summaries?${query}`), query).toMatchObject({
        status: 422,
        body: { code: "WV-FILTER" },
      });
    for (const query of invalidQueries)
      expect(answerRunViews(`/run-summaries?${query}`), query).toMatchObject({
        status: 422,
        body: { code: "WV-VALIDATION" },
      });
  });
  it("accepts the first and last representable instants and refuses offsets that leave them", () => {
    for (const query of [
      "started_after=0001-01-01T00:00:00Z",
      "started_after=0001-01-01T01:00:00%2B01:00",
      "started_before=9999-12-31T23:59:59.999999Z",
      "started_before=9999-12-31T22:59:59-01:00",
    ])
      expect(answerRunViews(`/run-summaries?${query}`)?.status, query).toBe(
        200,
      );
    for (const query of [
      "started_after=0001-01-01T00:00:00%2B00:01",
      "started_before=9999-12-31T23:59:59-00:01",
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

describe("run views keys and cursors", () => {
  it("matches upper-case run IDs as the server normalizes them", async () => {
    const children = await listRunSummaries(api, {
      caller_run_id: fixtureIds.failed.toUpperCase(),
    });
    expect(ids(children)).toEqual([fixtureIds.child]);
    const steps = await listRunSteps(api, fixtureIds.failed.toUpperCase());
    expect(steps.items).toHaveLength(7);
  });
  it("keeps a 200-character business key out of the cursor and under the 2048-character cap", async () => {
    const businessKey = `INV-${"x".repeat(196)}`;
    const correlationKey = `CORR-${"y".repeat(195)}`;
    const shared = {
      ...runViewsFixture,
      runs: runViewsFixture.runs.map((run) =>
        run.summary.id === fixtureIds.failed ||
        run.summary.id === fixtureIds.retry
          ? {
              ...run,
              summary: {
                ...run.summary,
                business_key: businessKey,
                correlation_key: correlationKey,
              },
            }
          : run,
      ),
    };
    const sharedApi = fakeRunViewsApi(shared);
    const query = {
      business_key: businessKey,
      correlation_key: correlationKey,
      limit: 1,
    };
    const first = await listRunSummaries(sharedApi, query);
    expect(ids(first)).toHaveLength(1);
    const cursor = first.next_cursor!;
    expect(cursor.length).toBeLessThanOrEqual(2048);
    expect(decodeCursor(cursor)).not.toContain("INV-");
    expect(decodeCursor(cursor)).not.toContain("CORR-");
    const second = await listRunSummaries(sharedApi, { ...query, cursor });
    expect(ids(second)).toHaveLength(1);
    expect(ids(second)[0]).not.toBe(ids(first)[0]);
    expect(second.next_cursor).toBeNull();
  });
  it("binds step and log cursors to their static step and level filters", async () => {
    const sends = await listRunSteps(api, fixtureIds.failed, {
      step: "send",
      limit: 2,
    });
    await expect(
      listRunSteps(api, fixtureIds.failed, {
        limit: 2,
        cursor: sends.next_cursor!,
      }),
    ).rejects.toMatchObject({ status: 422, code: "WV-VALIDATION" });
    const warnings = await listRunLogs(api, fixtureIds.failed, {
      level: "warning",
      limit: 1,
    });
    await expect(
      listRunLogs(api, fixtureIds.failed, {
        limit: 1,
        cursor: warnings.next_cursor!,
      }),
    ).rejects.toMatchObject({ status: 422, code: "WV-VALIDATION" });
  });
});

// Each string goes through parse_query (Python) and through the fake. The
// fake may be stricter only on forms the server accepts, so none of these
// strings uses one. Foreign well-formed cursors stay out: the server checks
// those after parsing, in the handler.
describe.skipIf(!pythonAvailable)("query decisions match parse_query", () => {
  const verdict = (answer: FakeAnswer | null) =>
    answer?.status === 200
      ? "ok"
      : String((answer?.body as { code?: string } | undefined)?.code);
  const differences = (model: QueryModel, path: string, queries: string[]) => {
    const expected = classifyQueriesWithPython(queries, model);
    return queries.flatMap((query, index) => {
      const actual = verdict(answerRunViews(`${path}?${query}`));
      return actual === expected[index]
        ? []
        : [{ query, fake: actual, python: expected[index] }];
    });
  };
  const summaryQueries = [
    "",
    "include_test=true&order=updated_desc&limit=100",
    "status=queued&status=failed&status=queued",
    "status=queued&status=queued&status=queued&status=queued&status=queued&status=queued&status=queued&status=queued",
    "workflow=invoice-approval&version=1.0.0%2Bbuild.5",
    "workflow=invoice-approval&version=1.0.0-rc.1%2Bbuild.5",
    "workflow=invoice-approval&version=1.0.0-0.3.7",
    "workflow=invoice-approval&version=1.0.0-0A",
    "workflow=invoice-approval&version=1.0.0%2Bbuild.00",
    "workflow=a.b-c_d",
    "started_after=2026-10-07T12:00:00%2B02:00&started_before=2026-10-07T13:00:00%2B02:00",
    "started_after=2026-10-07T12:00:00%2B23:59",
    "started_after=2026-10-07T12:00:00-00:00",
    "started_after=2026-10-07t12:00:00z",
    "started_after=2026-10-07T12:00:00.123456789Z",
    "started_after=2026-10-07T12:00:00.0000009Z&started_before=2026-10-07T12:00:00.0000019Z",
    "started_after=2026-01-01T00:00:00.000001Z&started_before=2027-02-05T00:00:00.000001Z",
    "started_after=2028-02-29T00:00:00Z",
    "started_after=0001-01-01T00:00:00Z",
    "started_after=0001-01-01T01:00:00%2B01:00",
    "started_before=9999-12-31T23:59:59.999999Z",
    "started_before=9999-12-31T23:59:59.9999999Z",
    "started_before=9999-12-31T22:59:59-01:00",
    "started_after=9999-12-31T23:59:59Z&started_before=9999-12-31T23:59:59.999999Z",
    "started_after=0001-01-01T23:59:59-23:59",
    "started_after=0001-01-02T00:00:00%2B23:59",
    "caller_run_id=1C0D7F8A-4D66-4E7C-8B62-5A2C1D3E8F12",
    "order=started_asc&include_archived=true",
    "limit=0100",
    ...contradictoryQueries,
    "started_after=2026-10-07T12:00:00.0000004Z&started_before=2026-10-07T12:00:00.0000006Z",
    "started_after=2026-10-07T12:00:00Z&started_before=2026-10-07T12:00:00Z",
    "started_after=2026-01-01T00:00:00Z&started_before=2027-02-05T00:00:00.000001Z",
    "started_after=2026-01-01T00:00:00Z&started_before=2027-02-05T00:00:01Z",
    "origin=test&cursor=AAAA",
    ...invalidQueries,
    "started_after=2026-13-01T00:00:00Z",
    "started_after=2026-10-07T24:00:00Z",
    "started_after=2026-10-07T12:00:60Z",
    "started_after=2026-02-30T00:00:00Z",
    "started_after=2027-02-29T00:00:00Z",
    "started_after=0000-01-01T00:00:00Z",
    "started_after=9999-12-31T23:59:59-23:59",
    "started_after=9999-12-31T23:59:59-01:00",
    "started_before=9999-12-31T23:59:59-00:01",
    "started_after=0001-01-01T00:00:00%2B23:59",
    "started_after=0001-01-01T00:00:00%2B00:01",
    "started_before=0001-01-01T00:00:00%2B00:01",
    "started_after=0001-01-01T00:00:00%2B24:00",
    "started_after=2026-04-31T00:00:00Z",
    "started_after=2026-00-10T00:00:00Z",
    "started_after=2026-10-00T00:00:00Z",
    "started_after=2026-10-07T12:60:00Z",
    "started_after=2026-10-07T12:00:00.Z",
    "started_after=2026-10-07T12:00:00%2B24:00",
    "started_after=2026-10-07T12:00:00%2B00:60",
    "started_after=2026-10-07T12:00:00Z&started_before=2026-13-01T00:00:00Z",
    "status=queued&status=queued&status=queued&status=queued&status=queued&status=queued&status=queued&status=queued&status=queued",
    "workflow=invoice-approval&version=1.0.0-01",
    "workflow=invoice-approval&version=1.0.0-1.01",
    "workflow=invoice-approval&version=1.0.0-a..b",
    "workflow=invoice-approval&version=1.0.0%2Ba..b",
    "workflow=invoice-approval&version=1.0.0+a..b",
    "workflow=invoice-approval&version=1.0.0%2B",
    "workflow=invoice-approval&version=1.0.0-",
    "workflow=invoice-approval&version=01.0.0",
    "constructor=1",
    "__proto__=1",
    "toString=1",
    "hasOwnProperty=1",
    "caller_run_id=1c0d7f8a-4d66-4e7c-8b62-5a2c1d3e8f1",
    "origin=test&cursor=!!",
    "version=1.0.0&cursor=%21%21",
    "cursor=%20",
    "include_test=true&workflow=a%20b",
    "workflow=%20a",
  ];
  const stepQueries = [
    "",
    "step=send",
    "include=output",
    "limit=500",
    "step=send&include=output&limit=2",
    "step=send%5B3%5D",
    "step=",
    "limit=501",
    "limit=0",
    "include=raw",
    "cursor=!!",
    "cursor=",
    "constructor=1",
    "__proto__=1",
    "include=output&cursor=!!",
    "step=send&limit=0",
  ];
  const logQueries = [
    "",
    "level=debug",
    "source=engine",
    "node_id=support",
    "level=warning&source=ai&node_id=a.b",
    "limit=500",
    "level=fatal",
    "source=ui",
    "node_id=support%23%31",
    "limit=501",
    "toString=1",
    "cursor=!!",
    "level=",
    "level=DEBUG",
    "level=warning&level=error",
  ];
  it("answers each run summary query string as parse_query does", () => {
    const expected = classifyQueriesWithPython(summaryQueries);
    expect(new Set(expected)).toEqual(
      new Set(["ok", "WV-FILTER", "WV-VALIDATION"]),
    );
    expect(
      differences("RunSummaryQuery", "/run-summaries", summaryQueries),
    ).toEqual([]);
  });
  it("answers each run step and run log query string as parse_query does", () => {
    expect(
      differences(
        "RunStepQuery",
        `/runs/${fixtureIds.failed}/steps`,
        stepQueries,
      ),
    ).toEqual([]);
    expect(
      differences("RunLogQuery", `/runs/${fixtureIds.failed}/logs`, logQueries),
    ).toEqual([]);
  });
});
