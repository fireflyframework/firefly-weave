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
import { keptAnswer } from "./browser/platform-answer";

const answer = (status: number, body: unknown) => ({
  status: () => status,
  json: async () => body,
});

describe("the platform answer Studio keeps", () => {
  it("skips a capacity refusal, which Studio sends again", async () => {
    for (const code of [
      "WV-OPERATION-CAPACITY",
      "WV-REQUEST-CAPACITY",
      "WV-STUDIO-BUSY",
    ])
      expect(await keptAnswer(answer(429, { status: 429, code }))).toBe(false);
  });
  it("keeps every other answer, a page limit and an unreadable 429 too", async () => {
    expect(await keptAnswer(answer(200, { items: [] }))).toBe(true);
    expect(await keptAnswer(answer(403, { code: "WV-FORBIDDEN" }))).toBe(true);
    expect(
      await keptAnswer(answer(429, { status: 429, code: "WV-PAGE-LIMIT" })),
    ).toBe(true);
    const unreadable = {
      status: () => 429,
      json: () => Promise.reject(new SyntaxError("not JSON")),
    };
    expect(await keptAnswer(unreadable)).toBe(true);
  });
});
