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
  timeAbsolute,
  timeIso,
  timeRelative,
} from "../src/app/operate/operate-time";

const now = new Date("2026-10-08T12:00:00Z");

describe("the times on the Operate pages", () => {
  it("gives a <time> its ISO datetime, or none for a value that isn't a date", () => {
    expect(timeIso("2026-10-08T11:58:00Z")).toBe("2026-10-08T11:58:00.000Z");
    expect(timeIso("not a date")).toBeNull();
    expect(timeIso("")).toBeNull();
  });

  it("titles a <time> with the weekday, day, month and 24-hour time", () => {
    expect(timeAbsolute("2026-10-08T11:58:00Z", "UTC")).toBe(
      "Thu 8 Oct, 11:58",
    );
    expect(timeAbsolute("not a date")).toBe("");
  });

  it("says how long ago, and Unknown for a value that isn't a date", () => {
    expect(timeRelative("2026-10-08T11:58:00Z", "Unknown", now)).toBe(
      "2 min ago",
    );
    expect(timeRelative("2026-10-08T11:59:50Z", "Unknown", now)).toBe(
      "just now",
    );
    expect(timeRelative("2026-10-08T09:00:00Z", "Unknown", now)).toBe(
      "3 hours ago",
    );
    expect(timeRelative("not a date", "Unknown", now)).toBe("Unknown");
    expect(timeRelative("not a date")).toBe("Unknown");
  });

  it("can leave a value that isn't a date blank instead", () => {
    expect(timeRelative("2026-10-08T11:58:00Z", "", now)).toBe("2 min ago");
    expect(timeRelative("not a date", "", now)).toBe("");
  });
});
