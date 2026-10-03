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
  absoluteTime,
  dateWithRelative,
  durationText,
  isoTime,
  relativeTime,
  shortId,
  toDate,
  workspaceFallback,
  workspaceLong,
  workspaceShort,
} from "../src/app/format";

const now = new Date("2026-10-02T16:00:00Z");

describe("dates", () => {
  it("says how long ago or how soon", () => {
    expect(relativeTime("2026-10-02T15:58:00Z", now)).toBe("2 min ago");
    expect(relativeTime("2026-10-02T15:59:50Z", now)).toBe("just now");
    expect(relativeTime("2026-10-02T13:00:00Z", now)).toBe("3 hours ago");
    expect(relativeTime("2026-10-02T15:00:00Z", now)).toBe("1 hour ago");
    expect(relativeTime("2026-10-03T17:00:00Z", now)).toBe("in 1 day");
    expect(relativeTime("2026-09-28T16:00:00Z", now)).toBe("4 days ago");
    expect(relativeTime("not a date", now)).toBe("");
    expect(relativeTime(undefined, now)).toBe("");
  });

  it("writes weekday, day, month and a 24-hour time", () => {
    expect(absoluteTime("2026-10-03T17:00:00Z", "UTC")).toBe(
      "Sat 3 Oct, 17:00",
    );
    expect(dateWithRelative("2026-10-03T17:00:00Z", now, "UTC")).toBe(
      "Sat 3 Oct, 17:00 (in 1 day)",
    );
    expect(dateWithRelative("", now)).toBe("");
    expect(isoTime("2026-10-03T17:00:00Z")).toBe("2026-10-03T17:00:00.000Z");
    expect(toDate("")).toBeNull();
  });

  it("writes durations people read at a glance", () => {
    expect(durationText(45_000)).toBe("45 s");
    expect(durationText(125_000)).toBe("2 min 5 s");
    expect(durationText(120_000)).toBe("2 min");
    expect(durationText(3_840_000)).toBe("1 h 4 min");
    expect(durationText(3 * 86_400_000)).toBe("3 days");
    expect(durationText(-1)).toBe("");
  });
});

describe("identifiers and workspaces", () => {
  it("shortens IDs to 8 characters", () => {
    expect(shortId("700587c4-1111-4111-8111-111111111111")).toBe("700587c4");
    expect(shortId("01HZY3K8Q2V7TQ6M4N1W9XGZ5B")).toBe("01HZY3K8");
    expect(shortId(null)).toBe("");
  });

  it("keeps a readable ID whole instead of cutting a word", () => {
    expect(shortId("development")).toBe("development");
    expect(shortId("payments-eu")).toBe("payments-eu");
    expect(workspaceFallback("development")).toBe("Workspace development");
  });

  it("formats workspaces one way, with spaces around the slash", () => {
    const names = {
      tenant: "Acme",
      project: "Payments",
      environment: "Production",
    };
    expect(workspaceShort(names)).toBe("Payments / Production");
    expect(workspaceLong(names)).toBe("Acme / Payments / Production");
    expect(workspaceLong({ ...names, tenant: "" })).toBe(
      "Payments / Production",
    );
    expect(workspaceFallback("1a2b3c4d-0000-4000-8000-000000000000")).toBe(
      "Workspace 1a2b3c4d",
    );
  });
});
