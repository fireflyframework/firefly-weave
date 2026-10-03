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
import { describe, it, expect } from "vitest";
import { LumiConversation, unchangedDraft } from "../src/app/lumi/lumi-state";
describe("Lumi local conversation", () => {
  it("clears history and invalidates pending replies on identity or workspace change", () => {
    const state = new LumiConversation();
    state.sync("alice:development");
    state.turns.push({ role: "user", content: "private" });
    const generation = state.generation;
    expect(state.sync("alice:development")).toBe(false);
    expect(state.history()).toHaveLength(1);
    expect(state.sync("bob:development")).toBe(true);
    expect(state.history()).toEqual([]);
    expect(state.generation).toBeGreaterThan(generation);
    state.turns.push({ role: "assistant", content: "answer" });
    state.sync("bob:production");
    expect(state.history()).toEqual([]);
  });
  it("requires the exact captured source including unapplied source-editor edits", () => {
    const base = {
      opened: 1,
      revision: 4,
      source: "original",
      buffer: "original",
    };
    expect(unchangedDraft(base, { ...base })).toBe(true);
    for (const key of ["opened", "revision", "source", "buffer"] as const) {
      expect(
        unchangedDraft(base, {
          ...base,
          [key]: typeof base[key] === "number" ? 99 : "changed",
        }),
      ).toBe(false);
    }
  });
  it("bounds outgoing history to sixteen text-only messages", () => {
    const state = new LumiConversation();
    for (let i = 0; i < 20; i++)
      state.turns.push({
        role: "user",
        content: String(i),
        followUps: ["not sent"],
      });
    expect(state.history()).toHaveLength(16);
    expect(state.history()[0]).toEqual({ role: "user", content: "4" });
  });
});
