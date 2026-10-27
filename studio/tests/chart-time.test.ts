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
import { beforeAll, describe, expect, it } from "vitest";

// West of UTC, where a UTC day starts the evening before.
process.env["TZ"] = "America/Los_Angeles";
let model: typeof import("../src/app/operate/metrics/chart-model");
beforeAll(async () => {
  model = await import("../src/app/operate/metrics/chart-model");
});

describe("Metrics time labels west of UTC", () => {
  it("names a daily bucket by its UTC day, never the day before", () => {
    expect(new Date("2026-10-25T00:00:00Z").getDate()).toBe(24);
    expect(model.timeLabel("2026-10-25T00:00:00Z", "1d")).toBe("Oct 25");
  });

  it("dates hourly buckets when the range spans more than a day", () => {
    expect(model.timeLabel("2026-10-25T15:00:00Z", "1h")).toBe("08:00");
    expect(model.timeLabel("2026-10-25T15:00:00Z", "1h", true)).toBe(
      "Oct 25, 08:00",
    );
    expect(
      model.spansDays([
        ["2026-10-25T15:00:00Z", 1],
        ["2026-10-26T15:00:00Z", 1],
      ]),
    ).toBe(true);
    expect(
      model.spansDays([
        ["2026-10-25T15:00:00Z", 1],
        ["2026-10-25T20:00:00Z", 1],
      ]),
    ).toBe(false);
  });
});
