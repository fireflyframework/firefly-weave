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
// Log entries reach the screen only through their typed readers: a withheld
// row stays a row without text, an entry with a field of the wrong shape is
// unreadable rather than half shown, and the pages and queries Studio uses
// are the ones the Python contract accepts.
import { execFileSync } from "node:child_process";
import { delimiter, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { describeError } from "../src/app/errors";
import {
  executionLogPath,
  readExecutionLogPage,
  readRunLogPage,
  runLogPath,
} from "../src/app/operate/logs/logs-api";
import type {
  ExecutionLogEntry,
  ExecutionLogQuery,
  RunLogEntry,
} from "../src/app/operate/run-contracts";
import { pythonAvailable, python, repository } from "./python-path";

const RUN = "1c0d7f8a-5b2e-4c1d-9a3f-2e6b7c8d9e01";
const entry = (facts: Partial<RunLogEntry> = {}): RunLogEntry => ({
  id: "6a8f2c10-1d3e-4b5f-8a9b-0c1d2e3f4a5b",
  at: "2026-11-18T10:41:58.611Z",
  source: "worker",
  level: "info",
  code: "CUSTOMER.LOOKED_UP",
  message: "Looked up 1 customer in billing-db",
  node_id: "fetch",
  instance_key: "",
  attempt: 1,
  worker_id: "0f1e2d3c-4b5a-4968-8776-655443322110",
  fields: { customers: 1, region: "EU", cached: false, note: null },
  redactions: 1,
  truncated: false,
  ...facts,
});
const across = (facts: Partial<ExecutionLogEntry> = {}): ExecutionLogEntry => ({
  ...entry(),
  run_id: RUN,
  workflow_name: "invoice-approval",
  workflow_version: "1.3.0",
  test: false,
  ...facts,
});
const withheld = {
  id: "7b9e3d21-2e4f-4c60-9bac-1d2e3f4a5b6c",
  unavailable: true,
  omissions: [{ path: "", reason: "classification_unavailable" }],
};

describe("reading log pages", () => {
  it("keeps an entry's text, flags and run as the platform sent them", () => {
    const page = readExecutionLogPage({
      items: [across({ redactions: 2, truncated: true })],
      next_cursor: "abc_-1",
    });
    expect(page.nextCursor).toBe("abc_-1");
    expect(page.rows).toEqual([
      {
        kind: "entry",
        id: across().id,
        entry: entry({ redactions: 2, truncated: true }),
        run: {
          id: RUN,
          workflow: "invoice-approval",
          version: "1.3.0",
          test: false,
        },
      },
    ]);
  });
  it("reads a withheld run's entry as a row without text", () => {
    const page = readExecutionLogPage({ items: [withheld], next_cursor: null });
    expect(page.rows).toEqual([{ kind: "withheld", id: withheld.id }]);
  });
  it("reads an entry with a field of the wrong shape as unreadable, never half shown", () => {
    const broken: unknown[] = [
      { ...across(), redactions: "2" },
      { ...across(), truncated: 1 },
      { ...across(), level: "fatal" },
      { ...across(), source: "kernel" },
      { ...across(), fields: { nested: { value: 1 } } },
      { ...across(), fields: ["a"] },
      { ...across(), message: 7 },
      { ...across(), at: "yesterday" },
      { ...across(), run_id: 4 },
      { ...across(), test: "false" },
      { ...across(), attempt: 0 },
      { ...across(), redactions: -1 },
      { ...withheld, unavailable: "yes" },
    ];
    const page = readExecutionLogPage({
      items: broken.map((item, index) => ({
        ...(item as object),
        id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
      })),
      next_cursor: null,
    });
    expect(page.rows.map((row) => row.kind)).toEqual(
      broken.map(() => "unreadable"),
    );
  });
  it("leaves out an item without an ID and a cursor that isn't one", () => {
    expect(
      readExecutionLogPage({ items: [{ message: "x" }, 3], next_cursor: 9 }),
    ).toEqual({ rows: [], nextCursor: null });
  });
  it("refuses an answer that isn't a page of entries, with a support code", () => {
    for (const answer of [
      "<html>",
      { message: "HTTP 200" },
      { items: {} },
      null,
    ])
      for (const read of [readExecutionLogPage, readRunLogPage]) {
        let thrown: unknown;
        try {
          read(answer);
        } catch (error) {
          thrown = error;
        }
        expect(describeError(thrown), JSON.stringify(answer)).toEqual({
          status: 502,
          code: "WV-STUDIO-ANSWER",
          message: "Studio couldn't read the platform's answer.",
        });
      }
  });
  it("reads a run's entries without a run of their own", () => {
    const page = readRunLogPage({ items: [entry()], next_cursor: null });
    expect(page.rows).toEqual([
      { kind: "entry", id: entry().id, entry: entry(), run: null },
    ]);
  });
});

describe("log addresses", () => {
  it("asks for a run's entries and the entries across runs with their filters", () => {
    expect(
      runLogPath("/env", RUN, {
        level: "warning",
        node_id: "send",
        limit: 500,
      }),
    ).toBe(`/env/runs/${RUN}/logs?level=warning&node_id=send&limit=500`);
    expect(
      executionLogPath("/env", {
        from: "2026-11-18T09:54:00.000+00:00",
        to: "2026-11-18T10:54:00.000Z",
        contains: "50% off & more",
        limit: 100,
      }),
    ).toBe(
      "/env/execution-logs?from=2026-11-18T09%3A54%3A00.000%2B00%3A00" +
        "&to=2026-11-18T10%3A54%3A00.000Z&contains=50%25%20off%20%26%20more&limit=100",
    );
  });
});

const pythonOptions = {
  cwd: repository,
  encoding: "utf8" as const,
  timeout: 180_000,
  env: {
    ...process.env,
    PYTHONPATH: [resolve(repository, "src"), process.env.PYTHONPATH]
      .filter(Boolean)
      .join(delimiter),
  },
};
const contractScript = `
import json, sys
from starlette.datastructures import QueryParams
from firefly_weave.api.transport import parse_query
from firefly_weave.contracts import run_views as v
from firefly_weave.definitions.models import CatalogError
data = json.load(sys.stdin)
for page in data["pages"]:
    v.ExecutionLogPage.model_validate_json(json.dumps(page))
def verdict(raw):
    try:
        parse_query(QueryParams(raw), v.ExecutionLogQuery)
        return "ok"
    except CatalogError as error:
        return error.code
    except ValueError:
        return "WV-VALIDATION"
print(json.dumps([verdict(raw) for raw in data["queries"]]))
`;
const queryOf = (query: ExecutionLogQuery) =>
  executionLogPath("", query).replace(/^\/execution-logs\??/, "");

describe.skipIf(!pythonAvailable())(
  "log pages and queries against the Python contract",
  () => {
    it("accepts the pages Studio reads and the queries it sends, and refuses a long message search", () => {
      const verdicts = JSON.parse(
        execFileSync(python, ["-c", contractScript], {
          ...pythonOptions,
          input: JSON.stringify({
            pages: [
              {
                items: [
                  across(),
                  withheld,
                  across({
                    id: "8c0f4e32-3f50-4d71-8cbd-2e3f4a5b6c7d",
                    source: "engine",
                    code: "SMTP_REJECTED",
                    message: "send failed: SMTP_REJECTED",
                    attempt: null,
                    worker_id: null,
                    fields: {},
                    redactions: 0,
                  }),
                ],
                next_cursor: "abc",
              },
            ],
            queries: [
              queryOf({
                from: "2026-11-18T09:54:00.000Z",
                to: "2026-11-18T10:54:00.000Z",
                level: "warning",
                limit: 100,
              }),
              queryOf({
                from: "2026-11-18T04:54:00.000Z",
                to: "2026-11-18T10:54:00.000Z",
                contains: "SMTP",
                workflow: "invoice-approval",
                node_id: "send",
                code: "SMTP_REJECTED",
                include_test: true,
                limit: 100,
              }),
              queryOf({
                from: "2026-11-11T10:54:00.000Z",
                to: "2026-11-18T10:54:00.000Z",
                limit: 100,
              }),
              queryOf({
                from: "2026-11-18T04:53:59.999Z",
                to: "2026-11-18T10:54:00.000Z",
                contains: "SMTP",
              }),
            ],
          }),
        }),
      ) as string[];
      expect(verdicts).toEqual(["ok", "ok", "ok", "WV-FILTER"]);
    });
  },
);
