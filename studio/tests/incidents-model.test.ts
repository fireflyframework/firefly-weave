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
  filterIncidents,
  incidentCodes,
  incidentFiltersFromQuery,
  incidentFiltersToQuery,
  incidentStatusLabel,
  incidentTone,
  lastEvents,
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
  const incidents: IncidentRecord[] = [
    incident({ id: "a" }),
    incident({ id: "b", status: "resolved", code: "WV-TASK-FAILED" }),
    incident({ id: "c", status: "closed" }),
    { id: "d", unavailable: true },
  ];
  const ids = (filters = defaultIncidentFilters) =>
    filterIncidents(incidents, filters).map((item) => item.id);

  it("shows active incidents by default and every status on request", () => {
    expect(ids()).toEqual(["a"]);
    expect(ids({ status: "all", code: "" })).toEqual(["a", "b", "c", "d"]);
    expect(ids({ status: "resolved", code: "" })).toEqual(["b"]);
    expect(ids({ status: "all", code: "WV-TASK-FAILED" })).toEqual(["b"]);
    expect(incidentCodes(incidents)).toEqual([
      "WV-TASK-AMBIGUOUS",
      "WV-TASK-FAILED",
    ]);
  });

  it("round-trips through the address, leaving the default out", () => {
    expect(incidentFiltersToQuery(defaultIncidentFilters)).toBe("");
    const query = incidentFiltersToQuery({
      status: "all",
      code: "WV-TASK-FAILED",
    });
    expect(query).toBe("?status=all&code=WV-TASK-FAILED");
    expect(incidentFiltersFromQuery(query)).toEqual({
      status: "all",
      code: "WV-TASK-FAILED",
    });
    expect(incidentFiltersFromQuery("?status=open")).toEqual(
      defaultIncidentFilters,
    );
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
