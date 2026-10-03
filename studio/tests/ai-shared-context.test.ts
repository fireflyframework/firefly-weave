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
  sharedAiResults,
  withSharedAiResults,
  availableAiResults,
} from "../src/app/integrations/ai-shared-context";
import { visibleRefs } from "../src/app/forms/core/scope";

const workflow = {
  spec: {
    inputSchema: { type: "object" },
    llmProfiles: {
      small: {
        outputSchema: {
          type: "object",
          properties: { answer: { type: "string" } },
        },
      },
    },
    steps: [
      { id: "research", kind: "llm", profile: "small" },
      { id: "write", kind: "llm", profile: "small" },
      { id: "future", kind: "llm", profile: "small" },
    ],
  },
};
describe("explicit shared AI context within one execution", () => {
  it("offers only prior AI result roots visible at this step", () => {
    const refs = availableAiResults(visibleRefs(workflow, "write", "/context"));
    expect(refs.map((ref) => ref.stepId)).toEqual(["research"]);
  });
  it("retains arbitrary existing context and restores it after deselection", () => {
    const original = {
      op: { name: "coalesce", args: [{ ref: "/input/data" }, { literal: {} }] },
    };
    const refs = availableAiResults(visibleRefs(workflow, "write", "/context"));
    const result = withSharedAiResults(original, ["research"], refs);
    expect(result).toEqual({
      object: {
        data: original,
        sharedAiResults: {
          object: { research: { ref: "/steps/research/output/result" } },
        },
      },
    });
    expect(sharedAiResults(result)).toEqual(["research"]);
    expect(withSharedAiResults(result, [], refs)).toEqual(original);
    expect(original).not.toHaveProperty("object");
  });
  it("rejects unavailable, duplicate or excessive references", () => {
    const refs = availableAiResults(visibleRefs(workflow, "write", "/context"));
    expect(() =>
      withSharedAiResults({ literal: {} }, ["future"], refs),
    ).toThrow("available");
    expect(() =>
      withSharedAiResults({ literal: {} }, ["research", "research"], refs),
    ).toThrow("once");
    expect(() =>
      withSharedAiResults(
        { literal: {} },
        Array.from({ length: 17 }, (_, i) => String(i)),
        refs,
      ),
    ).toThrow("16");
  });
  it("does not mistake a hand-authored data object for its managed selection", () => {
    const context = {
      object: {
        data: { literal: {} },
        sharedAiResults: { literal: "custom value" },
      },
    };
    expect(sharedAiResults(context)).toEqual([]);
    expect(withSharedAiResults(context, [], [])).toEqual(context);
  });
});
