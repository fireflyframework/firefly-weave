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
// The schedule builder: the platform's UTC calendar rules, the next times a
// calendar fires, and the presets people choose from. The corpus is shared
// with tests/unit/test_schedule_corpus.py, which checks the platform agrees.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  describeCalendar,
  nextOccurrences,
  presetCron,
  readPreset,
  validateCalendar,
} from "../src/app/editor/triggers/schedule";

interface Corpus {
  from: string;
  accepted: { expression: string; from?: string; next: string[] }[];
  refused: string[];
}
const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/schedule-corpus.json"),
    "utf8",
  ),
) as Corpus;

const iso = (dates: Date[]) =>
  dates.map((date) => date.toISOString().replace(".000Z", "Z"));

describe("the shared calendar corpus", () => {
  it.each(corpus.accepted)(
    "accepts $expression and fires when the platform does",
    (item) => {
      expect(validateCalendar(item.expression)).toBeNull();
      expect(
        iso(
          nextOccurrences(
            item.expression,
            new Date(item.from ?? corpus.from),
            5,
          ),
        ),
      ).toEqual(item.next);
    },
  );

  it.each(corpus.refused)("refuses %j", (expression) => {
    expect(validateCalendar(expression)).toEqual(expect.any(String));
    expect(nextOccurrences(expression, new Date(corpus.from), 5)).toEqual([]);
  });
});

describe("what a refusal says", () => {
  it.each([
    [
      "0 9 * *",
      "Use five fields: minute, hour, day of month, month and day of week.",
    ],
    [
      "0 9 1 * 1",
      "Set the day of month or the day of week, not both. Leave the other as *.",
    ],
    ["0 24 * * *", "The hour must be between 0 and 23."],
    ["0 0 * * 7", "The day of week must be between 0 and 6 (Sunday is 0)."],
    [
      "5/15 * * * *",
      "Put a step of 1 or more after * or a range, like */15 or 0-30/5.",
    ],
    [
      "0 9 * * MON",
      "Use numbers, ranges like 1-5, lists like 1,15 and steps like */15.",
    ],
    ["0 0 30 2 *", "None of the chosen months has that day."],
  ])("explains %j", (expression, message) => {
    expect(validateCalendar(expression)).toBe(message);
  });
});

describe("naming a calendar", () => {
  it.each([
    ["0 9 * * 1", "Mondays 09:00 UTC"],
    ["0 9 * * 0", "Sundays 09:00 UTC"],
    ["30 18 * * 1-5", "Weekdays 18:30 UTC"],
    ["0 9 * * 0,6", "Weekends 09:00 UTC"],
    ["0 9 * * *", "Every day 09:00 UTC"],
    ["5 0 1 * *", "Day 1 of each month 00:05 UTC"],
    ["*/15 * * * *", "Every 15 minutes"],
    ["* * * * *", "Every minute"],
    ["0 0 1-7 * *", "0 0 1-7 * * UTC"],
  ])("names %j as %j", (expression, name) => {
    expect(describeCalendar(expression)).toBe(name);
  });
});

describe("presets", () => {
  it.each([
    [{ kind: "minutes", every: 15 }, "*/15 * * * *"],
    [{ kind: "daily", hour: 9, minute: 0 }, "0 9 * * *"],
    [{ kind: "weekly", day: 1, hour: 9, minute: 5 }, "5 9 * * 1"],
    [{ kind: "monthly", day: 31, hour: 23, minute: 59 }, "59 23 31 * *"],
    [{ kind: "custom", cron: "  0 0 1-7 * * " }, "0 0 1-7 * *"],
  ] as const)("builds %j as %j and reads it back", (preset, cron) => {
    expect(presetCron(preset)).toBe(cron);
    expect(readPreset(cron)).toEqual(
      preset.kind === "custom" ? { kind: "custom", cron } : preset,
    );
  });

  it("reads a calendar no preset makes as custom", () => {
    expect(readPreset("*/45 * * * *")).toEqual({
      kind: "custom",
      cron: "*/45 * * * *",
    });
    expect(readPreset("0 9 * * 1-5")).toEqual({
      kind: "custom",
      cron: "0 9 * * 1-5",
    });
  });
});
