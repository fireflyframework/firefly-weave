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
import { runViewsFixture } from "../src/app/operate/run-views.fixture";
import {
  MAX_RANGE_MS,
  defaultRunsFilters,
  handledText,
  liveDuration,
  originLabel,
  rangePresets,
  resolveRunsQuery,
  runFilterCount,
  runOutcome,
  runsFiltersFromQuery,
  runsFiltersToQuery,
  runsQuery,
  type RunsFilters,
} from "../src/app/operate/runs/runs-model";
import { classifyQueriesWithPython, pythonAvailable } from "./run-views-oracle";

const run = runViewsFixture.runs[0].summary;

describe("Runs model", () => {
  it("keeps private filters out of URLs and sends them to the API", () => {
    const filters = {
      ...defaultRunsFilters(),
      business_key: "INV secret",
      correlation_key: "customer secret",
      include_test: true,
    };
    expect(runsFiltersToQuery(filters)).not.toContain("secret");
    expect(
      new URLSearchParams(
        runsQuery(
          resolveRunsQuery(filters, new Date("2026-10-09T12:00:00Z")),
          null,
        ),
      ).get("business_key"),
    ).toBe("INV secret");
  });

  it("renews relative refresh bounds but freezes each pagination chain", () => {
    const filters = defaultRunsFilters();
    const first = resolveRunsQuery(filters, new Date("2026-10-09T12:00:00Z"));
    const next = new URLSearchParams(runsQuery(first, "opaque-cursor"));
    const refreshed = resolveRunsQuery(
      filters,
      new Date("2026-10-09T12:00:20Z"),
    );
    expect(next.get("started_before")).toBe(first.started_before);
    expect(next.get("cursor")).toBe("opaque-cursor");
    expect(refreshed.started_before).toBe("2026-10-09T12:00:20.000Z");
    expect(refreshed.started_after).toBe("2026-10-08T12:00:20.000Z");
    const absolute = {
      ...filters,
      timeWindow: {
        kind: "absolute" as const,
        started_after: "2026-10-01T00:00:00Z",
        started_before: "2026-10-02T00:00:00Z",
      },
    };
    expect(
      resolveRunsQuery(absolute, new Date("2026-10-10T00:00:00Z")),
    ).toEqual(resolveRunsQuery(absolute, new Date("2026-10-11T00:00:00Z")));
  });

  it("labels handled success without inventing a run status", () => {
    const handled = { ...run, status: "succeeded" as const, handled_errors: 2 };
    expect(runOutcome(handled)).toEqual({
      label: "Succeeded with errors",
      tone: "warning",
    });
    expect(runOutcome({ ...run, status: "succeeded" })).toEqual({
      label: "Succeeded",
      tone: "success",
    });
    // An older platform leaves the count out: no handled errors.
    const { handled_errors: _, ...older } = handled;
    expect(runOutcome(older)).toEqual({ label: "Succeeded", tone: "success" });
    expect(runOutcome({ ...run, status: "cancelled" })).toEqual({
      label: "Canceled",
      tone: "neutral",
    });
    // A running run says how many errors it handled, without a new status.
    expect(
      runOutcome({ ...run, status: "running", handled_errors: 2 }),
    ).toEqual({
      label: "Running",
      tone: "info",
    });
    expect(handledText({ ...run, status: "running", handled_errors: 2 })).toBe(
      "2 errors handled",
    );
    expect(handledText({ ...run, status: "running", handled_errors: 1 })).toBe(
      "1 error handled",
    );
    expect(
      handledText({ ...run, status: "succeeded", handled_errors: 2 }),
    ).toBe("");
    expect(handledText(older)).toBe("");
  });

  it("keeps a relative preset as a preset in the address, never as timestamps", () => {
    const week = {
      ...defaultRunsFilters(),
      timeWindow: { kind: "relative" as const, milliseconds: 7 * 86_400_000 },
    };
    expect(runsFiltersToQuery(week)).toBe("?range=7d");
    expect(runsFiltersFromQuery("?range=7d")).toEqual(week);
    for (const preset of rangePresets) {
      const filters: RunsFilters = {
        ...defaultRunsFilters(),
        timeWindow: { kind: "relative", milliseconds: preset.milliseconds },
      };
      const query = runsFiltersToQuery(filters);
      expect(query).not.toMatch(/started_(after|before)|\d{4}-\d{2}/);
      expect(runsFiltersFromQuery(query)).toEqual(filters);
    }
    // The default (the last 24 hours) leaves the address clean.
    expect(runsFiltersToQuery(defaultRunsFilters())).toBe("");
    expect(runsFiltersFromQuery("")).toEqual(defaultRunsFilters());
  });

  it("keeps a custom range exactly as given", () => {
    const custom: RunsFilters = {
      ...defaultRunsFilters(),
      timeWindow: {
        kind: "absolute",
        started_after: "2026-10-01T00:00:00+02:00",
        started_before: "2026-10-02T00:00:00Z",
      },
    };
    const query = runsFiltersToQuery(custom);
    expect(new URLSearchParams(query).get("started_after")).toBe(
      "2026-10-01T00:00:00+02:00",
    );
    expect(runsFiltersFromQuery(query)).toEqual(custom);
    expect(
      new URLSearchParams(
        runsQuery(resolveRunsQuery(custom, new Date()), null),
      ).get("started_after"),
    ).toBe("2026-10-01T00:00:00+02:00");
  });

  it("refuses an address window that is not a positive span of 400 days or less", () => {
    const fallback = defaultRunsFilters();
    for (const search of [
      "?range=0m",
      "?range=-5m",
      "?range=401d",
      "?range=NaNh",
      "?range=1e3d",
      "?range=forever",
      "?started_after=2026-10-02T00:00:00Z&started_before=2026-10-01T00:00:00Z",
      "?started_after=2025-01-01T00:00:00Z&started_before=2026-10-01T00:00:00Z",
      "?started_after=yesterday",
    ])
      expect(runsFiltersFromQuery(search).timeWindow, search).toEqual(
        fallback.timeWindow,
      );
    expect(runsFiltersFromQuery("?range=400d").timeWindow).toEqual({
      kind: "relative",
      milliseconds: MAX_RANGE_MS,
    });
    expect(runsFiltersFromQuery("?range=90m").timeWindow).toEqual({
      kind: "relative",
      milliseconds: 90 * 60_000,
    });
    expect(
      runsFiltersFromQuery("?started_after=2026-10-01T00:00:00Z").timeWindow,
    ).toEqual({ kind: "absolute", started_after: "2026-10-01T00:00:00Z" });
  });

  it("reads public filters from the address and never a private key", () => {
    const search =
      "?status=failed&status=waiting&status=bogus&workflow=invoice-approval&version=1.3.0" +
      "&origin=webhook&include_test=true&top_level_only=true&has_active_incident=true" +
      "&has_handled_errors=false&include_archived=true&order=started_asc" +
      "&business_key=INV-1&correlation_key=c-1";
    const filters = runsFiltersFromQuery(search);
    expect(filters).toEqual({
      ...defaultRunsFilters(),
      status: ["failed", "waiting"],
      workflow: "invoice-approval",
      version: "1.3.0",
      origin: "webhook",
      include_test: true,
      top_level_only: true,
      has_active_incident: true,
      has_handled_errors: false,
      include_archived: true,
      order: "started_asc",
    });
    expect(filters.business_key).toBeUndefined();
    expect(filters.correlation_key).toBeUndefined();
    // Values the platform would refuse never reach it.
    expect(
      runsFiltersFromQuery(
        "?origin=fax&order=random&version=1.0.0&caller_run_id=x",
      ),
    ).toEqual(defaultRunsFilters());
    // Round trip, with the private keys left behind on purpose.
    const query = runsFiltersToQuery({
      ...filters,
      business_key: "status=failed",
      correlation_key: "range=7d",
    });
    expect(runsFiltersFromQuery(query)).toEqual(filters);
    expect(query).not.toContain("business_key");
    expect(query).not.toContain("correlation_key");
  });

  it("counts the filters a person changed, private ones included", () => {
    expect(runFilterCount(defaultRunsFilters())).toBe(0);
    expect(
      runFilterCount({
        ...defaultRunsFilters(),
        status: ["failed"],
        business_key: "INV-1",
        include_test: true,
        timeWindow: { kind: "relative", milliseconds: 3_600_000 },
      }),
    ).toBe(4);
  });

  it("names origins in plain words", () => {
    expect(
      (
        [
          "manual",
          "webhook",
          "schedule",
          "broker",
          "email",
          "provider",
          "retry",
          "call",
          "test",
          null,
        ] as const
      ).map(originLabel),
    ).toEqual([
      "Manual",
      "Webhook",
      "Schedule",
      "Kafka",
      "Email",
      "App event",
      "Retry",
      "Called by a workflow",
      "Test",
      "Unknown",
    ]);
  });

  it("counts a duration up while the run is active", () => {
    const now = new Date("2026-10-07T11:30:42Z");
    const active = runViewsFixture.runs[4].summary;
    expect(active.status).toBe("running");
    expect(liveDuration(active, now)).toBe("42 s");
    expect(liveDuration(run, now)).toBe("42 s");
    expect(liveDuration({ ...run, duration_ms: null }, now)).toBe("—");
  });

  it.skipIf(!pythonAvailable)(
    "sends only queries the platform accepts, first pages and later ones",
    () => {
      const now = new Date("2026-10-09T12:00:00Z");
      const filters: RunsFilters[] = [
        defaultRunsFilters(),
        {
          ...defaultRunsFilters(),
          status: ["failed", "waiting"],
          workflow: "invoice-approval",
          version: "1.3.0",
          origin: "retry",
          include_test: true,
          top_level_only: true,
          has_active_incident: true,
          has_handled_errors: true,
          include_archived: true,
          business_key: "INV 1041",
          correlation_key: "c+1",
          order: "updated_desc",
        },
        ...rangePresets.map((preset) => ({
          ...defaultRunsFilters(),
          timeWindow: {
            kind: "relative" as const,
            milliseconds: preset.milliseconds,
          },
        })),
        {
          ...defaultRunsFilters(),
          timeWindow: {
            kind: "absolute" as const,
            started_after: "2026-10-01T00:00:00+02:00",
            started_before: "2026-10-02T00:00:00Z",
          },
        },
      ];
      const queries = filters.map((value) =>
        runsQuery(resolveRunsQuery(value, now), null).slice(1),
      );
      expect(classifyQueriesWithPython(queries)).toEqual(
        queries.map(() => "ok"),
      );
    },
  );
});
