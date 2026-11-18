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
// An answer applies only to the run, environment, person and filters it was
// asked for: a run switch, an environment switch or a filter change while a
// read is out drops that read's answer. An older platform is asked once.
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import {
  LogSearchStore,
  RunLogsStore,
  type LogsHost,
} from "../src/app/operate/logs/logs-store";
import type {
  ExecutionLogQuery,
  RunLogEntry,
} from "../src/app/operate/run-contracts";

const RUN_A = "aaaaaaaa-0000-4000-8000-000000000001";
const RUN_B = "bbbbbbbb-0000-4000-8000-000000000002";
const entry = (id: string, message = id): RunLogEntry => ({
  id,
  at: "2026-11-18T10:41:58.611Z",
  source: "worker",
  level: "info",
  code: null,
  message,
  node_id: "fetch",
  instance_key: "",
  attempt: 1,
  worker_id: null,
  fields: {},
  redactions: 0,
  truncated: false,
});
const across = (id: string) => ({
  ...entry(id),
  run_id: RUN_A,
  workflow_name: "invoice-approval",
  workflow_version: "1.3.0",
  test: false,
});
const page = (items: unknown[], next_cursor: string | null = null) => ({
  items,
  next_cursor,
});

interface Pending {
  path: string;
  answer(body: unknown): Promise<void>;
  fail(error: unknown): Promise<void>;
}
/** A platform whose answers the test gives, in any order. */
function platform(environment = "/env/one") {
  const pending: Pending[] = [];
  let scope = "scope-1";
  const host: LogsHost & { environment: string } = {
    environment,
    api: () => ({
      environment: host.environment,
      request: <T>(path: string) =>
        new Promise<T>((resolve, reject) => {
          pending.push({
            path,
            answer: async (body) => {
              resolve(body as T);
              await settle();
            },
            fail: async (error) => {
              reject(error);
              await settle();
            },
          });
        }),
    }),
    scope: () => scope,
  };
  return {
    host,
    pending,
    paths: () => pending.map((item) => item.path),
    switchScope(next: string, nextEnvironment = host.environment) {
      scope = next;
      host.environment = nextEnvironment;
    },
  };
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const messages = (rows: { kind: string }[]) =>
  rows.map((row) =>
    row.kind === "entry"
      ? (row as unknown as { entry: RunLogEntry }).entry.message
      : row.kind,
  );

describe("a run's logs", () => {
  it("drops the answer for a run that is no longer open", async () => {
    const { host, pending } = platform();
    const store = new RunLogsStore(host, new Set());
    void store.open(RUN_A);
    void store.open(RUN_B);
    await pending[1].answer(page([entry("b1", "from B")]));
    await pending[0].answer(page([entry("a1", "from A")]));
    const state = store.state();
    expect(state.kind).toBe("ready");
    expect(messages(state.kind === "ready" ? state.rows : [])).toEqual([
      "from B",
    ]);
  });
  it("drops an answer that arrives after the environment or person changed", async () => {
    const { host, pending, switchScope } = platform();
    const store = new RunLogsStore(host, new Set());
    void store.open(RUN_A);
    // The answer lands before the page has noticed the switch.
    switchScope("scope-2", "/env/two");
    await pending[0].answer(page([entry("a1", "from the first environment")]));
    expect(store.state().kind).toBe("loading");
  });
  it("keeps the newest filters' answer when they change while a read is out", async () => {
    const { host, pending } = platform();
    const store = new RunLogsStore(host, new Set());
    void store.open(RUN_A);
    void store.setFilters({ level: "error", step: "" });
    expect(pending[1].path).toBe(
      `/env/one/runs/${RUN_A}/logs?level=error&limit=500`,
    );
    await pending[1].answer(page([entry("e1", "errors only")]));
    await pending[0].answer(page([entry("a1", "everything")]));
    const state = store.state();
    expect(messages(state.kind === "ready" ? state.rows : [])).toEqual([
      "errors only",
    ]);
    expect(store.filters()).toEqual({ level: "error", step: "" });
  });
  it("opens each run on every level and step", async () => {
    const { host, pending } = platform();
    const store = new RunLogsStore(host, new Set());
    void store.open(RUN_A);
    await pending[0].answer(page([]));
    void store.setFilters({ level: "warning", step: "send" });
    void store.open(RUN_B);
    expect(pending.at(-1)!.path).toBe(`/env/one/runs/${RUN_B}/logs?limit=500`);
    expect(store.filters()).toEqual({ level: "", step: "" });
  });
  it("asks a platform that doesn't serve logs once, then says so without asking", async () => {
    const { host, pending } = platform();
    const unserved = new Set<string>();
    const store = new RunLogsStore(host, unserved);
    void store.open(RUN_A);
    await pending[0].fail(
      new ApiError(501, { code: "WV-UNAVAILABLE", message: "Not served" }),
    );
    expect(store.state().kind).toBe("unserved");
    const other = new RunLogsStore(host, unserved);
    await other.open(RUN_B);
    await other.refresh();
    expect(other.state().kind).toBe("unserved");
    expect(pending).toHaveLength(1);
  });
  it("reads again as many pages as are shown, so a refresh never takes back Load more", async () => {
    const { host, pending, paths } = platform();
    const store = new RunLogsStore(host, new Set());
    void store.open(RUN_A);
    await pending[0].answer(page([entry("a1")], "next1"));
    void store.loadMore();
    expect(paths()[1]).toBe(
      `/env/one/runs/${RUN_A}/logs?limit=500&cursor=next1`,
    );
    await pending[1].answer(page([entry("a2")]));
    let state = store.state();
    expect(messages(state.kind === "ready" ? state.rows : [])).toEqual([
      "a1",
      "a2",
    ]);
    void store.refresh();
    await pending[2].answer(page([entry("a1"), entry("a1b")], "again"));
    expect(paths()[3]).toBe(
      `/env/one/runs/${RUN_A}/logs?limit=500&cursor=again`,
    );
    await pending[3].answer(page([entry("a2"), entry("a3")]));
    state = store.state();
    expect(messages(state.kind === "ready" ? state.rows : [])).toEqual([
      "a1",
      "a1b",
      "a2",
      "a3",
    ]);
    expect(state.kind === "ready" && state.more).toBe(false);
  });
});

describe("the log across runs", () => {
  const query = (
    facts: Partial<ExecutionLogQuery> = {},
  ): ExecutionLogQuery => ({
    from: "2026-11-18T09:54:00.000Z",
    to: "2026-11-18T10:54:00.000Z",
    level: "warning",
    limit: 100,
    ...facts,
  });
  it("keeps the newest filters' answer when they change while a read is out", async () => {
    const { host, pending } = platform();
    const store = new LogSearchStore(host, new Set());
    void store.search(query());
    void store.search(query({ level: "error" }));
    await pending[1].answer(page([across("new")]));
    await pending[0].answer(page([across("old")]));
    expect(messages(store.rows())).toEqual(["new"]);
  });
  it("drops an answer that arrives after the environment or person changed", async () => {
    const { host, pending, switchScope } = platform();
    const store = new LogSearchStore(host, new Set());
    void store.search(query());
    switchScope("scope-2", "/env/two");
    await pending[0].answer(page([across("before")]));
    expect(store.rows()).toEqual([]);
    expect(store.state().kind).toBe("loading");
  });
  it("continues with the first page's own query, whatever the filters became", async () => {
    const { host, pending, paths } = platform();
    const store = new LogSearchStore(host, new Set());
    void store.search(query({ contains: "SMTP" }));
    await pending[0].answer(page([across("n1")], "c1"));
    void store.loadOlder();
    expect(paths()[1]).toBe(
      "/env/one/execution-logs?from=2026-11-18T09%3A54%3A00.000Z&to=2026-11-18T10%3A54%3A00.000Z" +
        "&level=warning&limit=100&contains=SMTP&cursor=c1",
    );
    await pending[1].answer(page([across("o1")]));
    expect(messages(store.rows())).toEqual(["n1", "o1"]);
    expect(store.more()).toBe(false);
  });
  it("drops an older page when new filters were read meanwhile", async () => {
    const { host, pending } = platform();
    const store = new LogSearchStore(host, new Set());
    void store.search(query());
    await pending[0].answer(page([across("n1")], "c1"));
    void store.loadOlder();
    void store.search(query({ level: "error" }));
    await pending[2].answer(page([across("e1")]));
    await pending[1].answer(page([across("o1")]));
    expect(messages(store.rows())).toEqual(["e1"]);
  });
  it("adds what Follow finds on top and keeps reading older entries from where it was", async () => {
    const { host, pending, paths } = platform();
    const store = new LogSearchStore(host, new Set());
    void store.search(query());
    await pending[0].answer(page([across("b"), across("a")], "c1"));
    void store.follow(
      query({
        from: "2026-11-18T09:54:10.000Z",
        to: "2026-11-18T10:54:10.000Z",
      }),
    );
    await pending[1].answer(page([across("c"), across("b")], "c2"));
    expect(messages(store.rows())).toEqual(["c", "b", "a"]);
    void store.loadOlder();
    expect(paths()[2]).toContain("to=2026-11-18T10%3A54%3A00.000Z");
    expect(paths()[2]).toContain("cursor=c1");
  });
  it("says when it last read, and passes every failure on to the shell", async () => {
    const { host, pending } = platform();
    const failures: unknown[] = [];
    const store = new LogSearchStore(
      host,
      new Set(),
      (error) => failures.push(error),
      () => 1_700,
    );
    void store.search(query());
    await pending[0].answer(page([across("a")]));
    expect(store.updatedAt()).toBe(1_700);
    const ended = new ApiError(401, {
      code: "WV-AUTH-REQUIRED",
      message: "Signed out",
    });
    void store.follow(query());
    await pending[1].fail(ended);
    expect(failures).toEqual([ended]);
  });
  it("shows an answer that isn't a page of entries as a failed read", async () => {
    const { host, pending } = platform();
    const store = new LogSearchStore(host, new Set());
    void store.search(query());
    await pending[0].answer("<html>");
    const state = store.state();
    expect(state.kind).toBe("error");
    expect(state.kind === "error" && state.error.code).toBe("WV-STUDIO-ANSWER");
    expect(store.rows()).toEqual([]);
  });
  it("shows a withheld run's entry as a row without text", async () => {
    const { host, pending } = platform();
    const store = new LogSearchStore(host, new Set());
    void store.search(query());
    await pending[0].answer(
      page([
        {
          id: "cccccccc-0000-4000-8000-000000000003",
          unavailable: true,
          omissions: [{ path: "", reason: "classification_unavailable" }],
        },
      ]),
    );
    expect(store.rows()).toEqual([
      { kind: "withheld", id: "cccccccc-0000-4000-8000-000000000003" },
    ]);
  });
  it("says when a read took too long, keeping entries only for the same filters", async () => {
    const slow = () =>
      new ApiError(503, {
        code: "WV-UNAVAILABLE",
        message:
          "Logs took too long to read. Narrow the time range or the filters, or try again.",
      });
    const { host, pending } = platform();
    const store = new LogSearchStore(host, new Set());
    void store.search(query());
    await pending[0].answer(page([across("kept")]));
    void store.follow(query());
    await pending[1].fail(slow());
    expect(store.state().kind).toBe("slow");
    expect(messages(store.rows())).toEqual(["kept"]);
    // Entries of other filters never stand in for the new ones.
    void store.search(query({ contains: "x" }));
    expect(store.rows()).toEqual([]);
    await pending[2].fail(slow());
    expect(store.state().kind).toBe("slow");
    expect(store.rows()).toEqual([]);
  });
});
