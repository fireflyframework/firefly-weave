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
// What Settings › Administration says: the platform's uptime counted on from
// the summary's observed time, its build, compatibility, a retention plan's
// counts and confirmation, telemetry by host only, retention periods in
// words, and the allowed domains it accepts.
import { describe, expect, it } from "vitest";
import {
  readCompatibility,
  readPlan,
  readPlatform,
  readTelemetry,
} from "../src/app/settings/administration-api";
import {
  buildText,
  compatibilityText,
  domainProblem,
  findingText,
  periodRows,
  planWords,
  telemetryText,
  uptimeText,
} from "../src/app/settings/administration-model";
import * as fixture from "./alerts-fixture";
import { checkAlertsWithPython, pythonAvailable } from "./alerts-oracle";

const summary = {
  observed_at: "2026-11-18T10:54:00Z",
  api: {
    version: "0.1.0a17",
    build: `sha256:26ab04e1${"0".repeat(56)}`,
    instance: "9b000000-0000-4000-8000-000000000001",
    uptime_seconds: 273_600,
    compatibility: { mode: "ready", findings: 0 },
  },
  health: [],
};
const plan = {
  id: "7a000000-0000-4000-8000-000000000001",
  scope: { tenant_id: "1b000000-0000-4000-8000-000000000001" },
  principal_id: fixture.ME,
  created_at: "2026-11-18T10:32:00Z",
  cutoff: "2026-11-11T10:32:00Z",
  expires_at: "2026-11-18T14:32:00Z",
  complete: true,
  candidates: [
    ...Array.from({ length: 12 }, (_, index) => ({
      id: `7b000000-0000-4000-8000-0000000000${String(index).padStart(2, "0")}`,
      revision: 1,
      reason: "expired_debug",
    })),
    ...Array.from({ length: 2 }, (_, index) => ({
      id: `7c000000-0000-4000-8000-0000000000${String(index).padStart(2, "0")}`,
      revision: 1,
      reason: "referenced",
    })),
  ],
};
const telemetry = {
  enabled: true,
  traces: { configured: true, host: "otel.acme.example" },
  metrics: {
    configured: true,
    host: "otel.acme.example",
    interval_seconds: 60,
  },
  dropped: 3,
};

describe("the platform card", () => {
  it("uptime counts locally from the summary's observed time", () => {
    const platform = readPlatform(summary)!;
    expect(platform.version).toBe("0.1.0a17");
    expect(buildText(platform.build)).toBe("26ab04e1");
    expect(buildText(null)).toBe("Not recorded");
    // 3 days 4 h when observed; 10 s later it has not reached the next unit.
    expect(uptimeText(platform, Date.parse("2026-11-18T10:54:10Z"))).toBe(
      "3 days 4 h",
    );
    expect(uptimeText(platform, Date.parse("2026-11-18T12:54:00Z"))).toBe(
      "3 days 6 h",
    );
    expect(
      uptimeText(
        { ...platform, uptimeSeconds: 754 },
        Date.parse("2026-11-18T10:54:00Z"),
      ),
    ).toBe("12 min");
  });
  it("reads nothing from a summary without its api section", () => {
    expect(readPlatform({ health: [] })).toBeNull();
  });
});

describe("compatibility", () => {
  it("says the mode and the findings, each in words", () => {
    const report = readCompatibility({
      policy: "weave/operations-v1",
      mode: "restricted",
      complete: true,
      inspected: 1204,
      checked_at: "2026-11-18T08:02:00Z",
      findings: [
        { kind: "worker", code: "worker_protocol_unsupported" },
        { kind: "connection", code: "something_newer" },
      ],
      findings_truncated: false,
    })!;
    expect(compatibilityText(report)).toEqual({
      text: "Restricted · 2 findings",
      tone: "warning",
    });
    expect(report.findings.map(findingText)).toEqual([
      "A worker speaks a worker protocol this version doesn't support.",
      "A connection: something_newer.",
    ]);
    expect(
      compatibilityText({ ...report, mode: "ready", findings: [] }),
    ).toEqual({ text: "Ready · 0 findings", tone: "success" });
  });
});

describe("retention plans", () => {
  it("a retention plan shows its counts before Apply, and Apply asks first", () => {
    const words = planWords(readPlan(plan)!, "UTC");
    expect(words).toEqual({
      deletable: 12,
      deletableText: "12 expired debug sessions",
      keptText: "2 debug sessions something still uses",
      expires: "Plan expires at 14:32",
      confirm:
        "Delete 12 expired debug sessions? This can't be undone. Run histories and the other records stay.",
      apply: "Delete 12",
    });
    expect(
      planWords(readPlan({ ...plan, candidates: [] })!, "UTC"),
    ).toMatchObject({
      deletable: 0,
      deletableText: "No expired debug sessions",
      keptText: "",
    });
  });
  it.skipIf(!pythonAvailable)(
    "reads only plans the platform's contract allows",
    () => {
      checkAlertsWithPython({
        RetentionPlan: [plan],
        OperationsTelemetry: [telemetry],
      });
      expect(readPlan({ ...plan, expires_at: "soon" })).toBeNull();
    },
  );
});

describe("telemetry", () => {
  it("telemetry shows the host only", () => {
    expect(telemetryText(readTelemetry(telemetry)!)).toBe(
      "On · traces to otel.acme.example · metrics to otel.acme.example every 60 s · 3 dropped",
    );
    // Anything that isn't a bare host never reaches the screen.
    expect(
      telemetryText(
        readTelemetry({
          ...telemetry,
          traces: {
            configured: true,
            host: "user:secret@otel.acme.example/v1",
          },
        })!,
      ),
    ).toBe(
      "On · traces to a configured endpoint · metrics to otel.acme.example every 60 s · 3 dropped",
    );
    expect(
      telemetryText(
        readTelemetry({
          enabled: false,
          traces: { configured: false, host: null },
          metrics: { configured: false, host: null, interval_seconds: null },
          dropped: 0,
        })!,
      ),
    ).toBe("Off");
  });
});

describe("retention periods", () => {
  it("names each record and how long it's kept", () => {
    expect(periodRows(fixture.retentionPeriods().items)).toEqual([
      ["Run statistics, 5-minute buckets", "2 days"],
      ["Run statistics, hourly", "35 days"],
      ["Run statistics, daily", "400 days"],
      ["Samples, per minute", "24 hours"],
      ["Samples, hourly", "35 days"],
      ["Change requests", "90 days"],
      ["Alerts", "30 days"],
      ["Alert history", "30 days"],
      ["Log entries", "7 days"],
    ]);
    expect(periodRows([{ record: "new_record", seconds: 5400 }])).toEqual([
      ["new_record", "90 minutes"],
    ]);
  });
});

describe("allowed domains", () => {
  it("accepts a domain once, and says what's wrong with any other", () => {
    expect(domainProblem("Vendor.Example", [])).toEqual({
      domain: "vendor.example",
    });
    expect(domainProblem("vendor.example", ["vendor.example"])).toEqual({
      problem: "vendor.example is already allowed.",
    });
    expect(domainProblem("https://vendor.example", [])).toEqual({
      problem: "Enter a domain only, such as example.org.",
    });
    expect(domainProblem("oncall@vendor.example", [])).toEqual({
      problem: "Enter a domain only, such as example.org.",
    });
    expect(
      domainProblem(
        "x.example",
        Array.from({ length: 20 }, (_, index) => `d${index}.example`),
      ),
    ).toEqual({ problem: "Allow up to 20 domains." });
  });
});
