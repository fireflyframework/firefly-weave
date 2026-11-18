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
import type { LogRow } from "../src/app/operate/logs/logs-api";
import {
  countText,
  entryLine,
  executionLogQuery,
  filterSummary,
  logFailure,
  mergeFollowed,
  runCountText,
  runLogQuery,
  searchProblem,
  stopsReads,
} from "../src/app/operate/logs/logs-model";
import { defaultLogsRoute } from "../src/app/operate/operate-routes";
import type { RunLogEntry } from "../src/app/operate/run-contracts";

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
  fields: {},
  redactions: 0,
  truncated: false,
  ...facts,
});
const row = (id: string): LogRow => ({
  kind: "entry",
  id,
  entry: entry({ id }),
  run: null,
});
const ids = (rows: LogRow[]) => rows.map((item) => item.id);

describe("one log entry on screen", () => {
  it("shows its time to the millisecond, level, source, step and message", () => {
    expect(entryLine(entry(), "UTC")).toEqual({
      time: "10:41:58.611",
      title: expect.stringContaining("2026"),
      datetime: "2026-11-18T10:41:58.611Z",
      level: "Info",
      tone: "info",
      source: "Worker",
      step: "fetch",
      message: "Looked up 1 customer in billing-db",
      flags: [],
      details: "CUSTOMER.LOOKED_UP",
    });
    const engine = entryLine(
      entry({
        source: "engine",
        level: "error",
        code: "SMTP_REJECTED",
        message: "send failed: SMTP_REJECTED",
        node_id: null,
        attempt: null,
        worker_id: null,
      }),
      "UTC",
    );
    expect([engine.level, engine.tone, engine.source, engine.step]).toEqual([
      "Error",
      "danger",
      "Engine",
      "—",
    ]);
    // The engine's words already hold its code.
    expect(engine.details).toBe("");
  });
  it("says how many values were hidden and that a long message was shortened", () => {
    expect(entryLine(entry({ redactions: 1 }), "UTC").flags).toEqual([
      { icon: "lock", text: "1 value hidden" },
    ]);
    expect(
      entryLine(entry({ redactions: 2, truncated: true }), "UTC").flags,
    ).toEqual([
      { icon: "lock", text: "2 values hidden" },
      { icon: "stale", text: "Shortened" },
    ]);
  });
  it("lists a worker's code and fields after the message, as they were sent", () => {
    expect(
      entryLine(
        entry({
          code: null,
          fields: {
            items: 3,
            order: "ORD-1041",
            cached: false,
            region: null,
            token: "[redacted]",
          },
        }),
        "UTC",
      ).details,
    ).toBe(
      "items=3  order=ORD-1041  cached=false  region=null  token=[redacted]",
    );
  });
  it("leaves out a field whose value the message already says", () => {
    expect(
      entryLine(
        entry({
          source: "connector",
          code: "HTTP.RESPONSE",
          message: "POST smtp-relay.acme.example answered 550 in 312 ms",
          fields: {
            method: "POST",
            host: "smtp-relay.acme.example",
            status: 550,
            duration_ms: 312,
          },
        }),
        "UTC",
      ).details,
    ).toBe("HTTP.RESPONSE");
    expect(
      entryLine(
        entry({
          message: "Looked up 1 customer in billing-db",
          fields: { customers: 1, attempt: 2, shard: "db1" },
        }),
        "UTC",
      ).details,
    ).toBe("CUSTOMER.LOOKED_UP  attempt=2  shard=db1");
  });
  it("names an iteration of a step after the step", () => {
    expect(
      entryLine(entry({ node_id: "notify", instance_key: "notify[2]" }), "UTC")
        .step,
    ).toBe("notify[2]");
  });
});

describe("what a run's Logs section asks for", () => {
  it("reads every level and every step unless the person narrows them", () => {
    expect(runLogQuery({ level: "", step: "" })).toEqual({ limit: 500 });
    expect(runLogQuery({ level: "warning", step: "send" }, "abc")).toEqual({
      level: "warning",
      node_id: "send",
      limit: 500,
      cursor: "abc",
    });
  });
  it("counts its entries and says where they come from", () => {
    expect(runCountText(8, false)).toBe(
      "8 entries · engine entries come from the run's history; worker and connector entries are kept for 7 days.",
    );
    expect(runCountText(1, false)).toMatch(/^1 entry · /);
    expect(runCountText(500, true)).toMatch(/^The first 500 entries · /);
  });
});

describe("what the Logs page asks for", () => {
  const now = new Date("2026-11-18T10:54:00.000Z");
  it("reads the last hour from Warning up, without test runs, by default", () => {
    expect(executionLogQuery(defaultLogsRoute, "", now)).toEqual({
      from: "2026-11-18T09:54:00.000Z",
      to: "2026-11-18T10:54:00.000Z",
      level: "warning",
      limit: 100,
    });
  });
  it("sends every filter the person set, and the message only when there is one", () => {
    expect(
      executionLogQuery(
        {
          range: "6h",
          level: "",
          workflow: "invoice-approval",
          step: "send",
          code: "SMTP_REJECTED",
          test: true,
        },
        "  SMTP ",
        now,
      ),
    ).toEqual({
      from: "2026-11-18T04:54:00.000Z",
      to: "2026-11-18T10:54:00.000Z",
      workflow: "invoice-approval",
      node_id: "send",
      code: "SMTP_REJECTED",
      contains: "SMTP",
      include_test: true,
      limit: 100,
    });
  });
  it("asks for a shorter range before it searches messages over more than 6 hours", () => {
    const sentence =
      "Narrow the time range to 6 hours or less to search messages.";
    expect(searchProblem("24h", "SMTP")).toBe(sentence);
    expect(searchProblem("7d", "x")).toBe(sentence);
    expect(searchProblem("6h", "SMTP")).toBe("");
    expect(searchProblem("24h", "  ")).toBe("");
    expect(searchProblem("1h", "x".repeat(201))).toBe(
      "Keep the message to 200 characters or fewer.",
    );
  });
  it("counts the entries of the range and says how long they are kept", () => {
    expect(countText(6, "1h", false)).toBe(
      "6 entries in the last hour · entries are kept for 7 days",
    );
    expect(countText(1, "15m", false)).toBe(
      "1 entry in the last 15 minutes · entries are kept for 7 days",
    );
    expect(countText(100, "7d", true)).toBe(
      "The newest 100 entries in the last 7 days · entries are kept for 7 days",
    );
  });
});

describe("the phone's filter summary", () => {
  it("names the defaults when nothing changed", () => {
    expect(filterSummary(defaultLogsRoute, "", false)).toBe(
      "the defaults (last hour, Warning or higher, test runs off)",
    );
  });
  it("names every filter that isn't at its default, the message included, and Follow", () => {
    expect(
      filterSummary(
        {
          range: "6h",
          level: "error",
          workflow: "invoice-approval",
          step: "send",
          code: "SMTP_REJECTED",
          test: true,
        },
        "SMTP",
        true,
      ),
    ).toBe(
      "last 6 hours · Error only · workflow invoice-approval · step send · code SMTP_REJECTED · message contains “SMTP” · test runs included · following every 10 seconds",
    );
    expect(filterSummary(defaultLogsRoute, "", true)).toBe(
      "the defaults (last hour, Warning or higher, test runs off) · following every 10 seconds",
    );
  });
});

describe("Follow", () => {
  it("puts new entries on top and keeps the ones below where they are", () => {
    const shown = [row("c"), row("b"), row("a")];
    expect(
      ids(mergeFollowed(shown, [row("e"), row("d"), row("c")], true).rows),
    ).toEqual(["e", "d", "c", "b", "a"]);
    expect(mergeFollowed(shown, [row("c"), row("b")], false).replaced).toBe(
      false,
    );
  });
  it("starts again from the newest when so many arrived that a gap would open", () => {
    const merged = mergeFollowed(
      [row("b"), row("a")],
      [row("d"), row("c")],
      true,
    );
    expect(ids(merged.rows)).toEqual(["d", "c"]);
    expect(merged.replaced).toBe(true);
  });
});

describe("what a failed log read shows", () => {
  const failed = (status: number, code: string, message = "Refused") =>
    logFailure(new ApiError(status, { code, message }));
  it("tells an older platform, a missing capability and a withheld run apart", () => {
    expect(failed(501, "WV-UNAVAILABLE").kind).toBe("unserved");
    expect(failed(403, "WV-FORBIDDEN").kind).toBe("refused");
    expect(failed(409, "WV-LEGACY-UNAVAILABLE").kind).toBe("withheld");
    expect(failed(422, "WV-IR-UNSUPPORTED").kind).toBe("withheld");
  });
  it("shows a slow read with the platform's own sentence", () => {
    const slow = failed(
      503,
      "WV-UNAVAILABLE",
      "Logs took too long to read. Narrow the time range or the filters, or try again.",
    );
    expect(slow).toEqual({
      kind: "slow",
      error: {
        status: 503,
        code: "WV-UNAVAILABLE",
        message:
          "Logs took too long to read. Narrow the time range or the filters, or try again.",
      },
    });
  });
  it("stops reading only for what no later read can change", () => {
    expect(stopsReads(failed(403, "WV-FORBIDDEN").kind)).toBe(true);
    expect(stopsReads(failed(409, "WV-LEGACY-UNAVAILABLE").kind)).toBe(true);
    expect(stopsReads(failed(501, "WV-UNAVAILABLE").kind)).toBe(true);
    // A slow read, any other failure and an answer can change at the next read.
    expect(stopsReads(failed(503, "WV-UNAVAILABLE").kind)).toBe(false);
    expect(stopsReads(failed(409, "WV-REVISION").kind)).toBe(false);
    expect(stopsReads("ready")).toBe(false);
  });
  it("shows anything else with its message and support code", () => {
    expect(failed(500, "WV-INTERNAL", "Broken").kind).toBe("error");
    expect(logFailure(new TypeError("Failed to fetch")).kind).toBe("error");
  });
});
