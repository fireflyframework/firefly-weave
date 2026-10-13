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
import {
  checkResolution,
  defaultIncidentFilters,
  incidentFilterCount,
  incidentFiltersFromQuery,
  incidentFiltersToQuery,
  incidentFromQuery,
  incidentQuery,
  incidentSuggestions,
  incidentStatusLabel,
  incidentTone,
  lastEvents,
  openedText,
  resolutionLabel,
  type IncidentRecord,
  type IncidentView,
} from "../src/app/operate/incidents/incidents-model";

const incident = (overrides: Partial<IncidentView> = {}): IncidentView => ({
  id: "a",
  run_id: "11111111-1111-4111-8111-111111111111",
  incident_key: "charge:1",
  node_id: "charge",
  generation: 1,
  origin_code: "WV-TASK-AMBIGUOUS",
  code: "WV-TASK-AMBIGUOUS",
  status: "active",
  revision: 3,
  external_effects_may_continue: false,
  ...overrides,
});

describe("incident words", () => {
  it("labels statuses with their tones and decisions in words", () => {
    expect([incidentStatusLabel("active"), incidentTone("active")]).toEqual([
      "Active",
      "danger",
    ]);
    expect([incidentStatusLabel("resolved"), incidentTone("resolved")]).toEqual(
      ["Resolved", "success"],
    );
    expect([incidentStatusLabel("closed"), incidentTone("closed")]).toEqual([
      "Closed",
      "neutral",
    ]);
    expect(resolutionLabel("retry_safe")).toBe("Retry the step");
    expect(resolutionLabel("accept_reconciled_result")).toBe(
      "Accept a verified result",
    );
    expect(resolutionLabel("terminate")).toBe("End the run");
  });
});

describe("Resolve incident checks", () => {
  const draft = {
    kind: "retry_safe" as const,
    reason: "Provider confirmed the call failed",
    evidence: "",
    output: "",
  };

  it("needs a decision and a reason of 2,000 characters or fewer", () => {
    expect(checkResolution({ ...draft, kind: "", reason: " " })).toEqual({
      problems: {
        kind: "Choose a decision.",
        reason: "Enter a reason for the audit log.",
      },
    });
    expect(checkResolution({ ...draft, reason: "x".repeat(2001) })).toEqual({
      problems: {
        reason: "Keep the reason to 2,000 characters or fewer.",
      },
    });
    expect(checkResolution(draft)).toEqual({
      request: { kind: "retry_safe", reason: draft.reason },
    });
  });

  it("needs evidence and a JSON result to accept a verified result", () => {
    const accept = { ...draft, kind: "accept_reconciled_result" as const };
    expect(checkResolution({ ...accept, output: "{oops" })).toEqual({
      problems: {
        evidence: "Enter where the verified result can be checked.",
        output:
          'Enter the verified result as JSON, for example {"approved": true}.',
      },
    });
    expect(
      checkResolution({
        ...accept,
        evidence: " provider:receipt:123 ",
        output: '{"rows": [1]}',
      }),
    ).toEqual({
      request: {
        kind: "accept_reconciled_result",
        reason: draft.reason,
        evidence_reference: "provider:receipt:123",
        output: { rows: [1] },
      },
    });
    // JSON null is a result too.
    expect(
      checkResolution({ ...accept, evidence: "ticket-9", output: "null" }),
    ).toEqual({
      request: {
        kind: "accept_reconciled_result",
        reason: draft.reason,
        evidence_reference: "ticket-9",
        output: null,
      },
    });
  });

  it("sends no output when the run ends or the step retries", () => {
    expect(
      checkResolution({ ...draft, kind: "terminate", output: '{"x": 1}' }),
    ).toEqual({ request: { kind: "terminate", reason: draft.reason } });
  });
});

describe("incident filters", () => {
  it("sends active incident and test filters to the first server page", () => {
    const query = new URLSearchParams(
      incidentQuery({ ...defaultIncidentFilters, includeTest: true }, null),
    );
    expect(query.getAll("status")).toEqual(["active"]);
    expect(query.get("include_test")).toBe("true");
    expect(query.has("cursor")).toBe(false);
  });

  it("maps every filter to the platform's names and continues with a cursor", () => {
    const query = new URLSearchParams(
      incidentQuery(
        {
          status: "all",
          code: "WV-TASK-FAILED",
          workflow: "invoice-approval",
          nodeId: "charge",
          openedAfter: "2026-10-01T00:00:00.000Z",
          openedBefore: "2026-10-08T00:00:00.000Z",
          includeTest: false,
          order: "opened_asc",
        },
        "next-page",
      ),
    );
    expect(Object.fromEntries(query)).toEqual({
      limit: "50",
      order: "opened_asc",
      include_test: "false",
      code: "WV-TASK-FAILED",
      workflow: "invoice-approval",
      node_id: "charge",
      opened_after: "2026-10-01T00:00:00.000Z",
      opened_before: "2026-10-08T00:00:00.000Z",
      cursor: "next-page",
    });
    expect(query.has("status")).toBe(false);
    expect(
      new URLSearchParams(incidentQuery(defaultIncidentFilters, null)).get(
        "order",
      ),
    ).toBe("opened_desc");
  });

  it("counts the filters that differ from active, newest first", () => {
    expect(incidentFilterCount(defaultIncidentFilters)).toBe(0);
    expect(
      incidentFilterCount({
        ...defaultIncidentFilters,
        status: "all",
        workflow: "w",
        includeTest: true,
        order: "opened_asc",
      }),
    ).toBe(3);
  });

  it("round-trips through the address, leaving the defaults out", () => {
    expect(incidentFiltersToQuery(defaultIncidentFilters)).toBe("");
    const filters = {
      status: "all" as const,
      code: "WV-TASK-FAILED",
      workflow: "invoice-approval",
      nodeId: "charge",
      openedAfter: "2026-10-01T00:00:00.000Z",
      openedBefore: "2026-10-08T00:00:00.000Z",
      includeTest: true,
      order: "id" as const,
    };
    const query = incidentFiltersToQuery(filters, "");
    expect(incidentFiltersFromQuery(query)).toEqual(filters);
    expect(incidentFiltersFromQuery("?status=open&order=newest")).toEqual(
      defaultIncidentFilters,
    );
    expect(
      incidentFiltersFromQuery("?opened_after=yesterday").openedAfter,
    ).toBe("");
  });

  it("keeps an open incident in the address, and only a valid ID", () => {
    const id = "91111111-1111-4111-8111-111111111111";
    const query = incidentFiltersToQuery(defaultIncidentFilters, id);
    expect(query).toBe(`?incident=${id}`);
    expect(incidentFromQuery(query)).toBe(id);
    expect(incidentFromQuery("?incident=../runs")).toBe("");
    expect(incidentFiltersFromQuery(query)).toEqual(defaultIncidentFilters);
  });

  it("suggests the codes, workflows and steps the loaded incidents carry", () => {
    const incidents: IncidentRecord[] = [
      incident({ id: "a", workflow: { name: "w2", version: "1.0.0" } }),
      incident({
        id: "b",
        code: "WV-TASK-FAILED",
        node_id: "ship",
        workflow: { name: "w1", version: "1.0.0" },
      }),
      { id: "d", unavailable: true },
    ];
    expect(incidentSuggestions(incidents)).toEqual({
      codes: ["WV-TASK-AMBIGUOUS", "WV-TASK-FAILED"],
      workflows: ["w1", "w2"],
      steps: ["charge", "ship"],
    });
  });

  it("keeps the last 20 engine events, types only", () => {
    const events = Array.from({ length: 25 }, (_, index) => ({
      sequence: index + 1,
      type: index % 2 ? "task_failed" : "task_scheduled",
      data: { secret: "never shown" },
    }));
    const lines = lastEvents(events);
    expect(lines).toHaveLength(20);
    expect(lines[0]).toEqual({ sequence: 6, type: "task_failed" });
    expect(JSON.stringify(lines)).not.toContain("never shown");
  });
});

describe("opening times", () => {
  const relative = (value: string) => `at ${value}`;
  it("accepts an incident without the estimate flag as exact", () => {
    // An earlier platform leaves the flag out entirely.
    const earlier: IncidentView = incident({
      opened_at: "2026-10-07T10:00:00Z",
    });
    expect("opened_at_estimated" in earlier).toBe(false);
    expect(earlier.opened_at_estimated ?? false).toBe(false);
    expect(openedText(earlier, relative)).toBe("at 2026-10-07T10:00:00Z");
  });
  it("marks an estimate, says Unknown without a time, and never invents one", () => {
    expect(
      openedText(
        incident({
          opened_at: "2026-10-07T10:00:00Z",
          opened_at_estimated: true,
        }),
        relative,
      ),
    ).toBe("at 2026-10-07T10:00:00Z (estimated)");
    expect(
      openedText(
        incident({
          opened_at: "2026-10-07T10:00:00Z",
          opened_at_estimated: false,
        }),
        relative,
      ),
    ).toBe("at 2026-10-07T10:00:00Z");
    expect(openedText(incident(), relative)).toBe("Unknown");
    expect(
      openedText(incident({ opened_at: "2026-10-07T10:00:00Z" }), () => ""),
    ).toBe("Unknown");
  });
});
