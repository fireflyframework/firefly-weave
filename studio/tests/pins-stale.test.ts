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
  contractOf,
  keepPinned,
  pinsView,
  stampedPin,
  staleText,
} from "../src/app/editor/state/pins-stale";
import {
  fingerprint,
  fingerprints,
  type StepFingerprints,
} from "../src/app/editor/state/fingerprints";
import { emptyTestData, withPin } from "../src/app/editor/state/test-data-ops";
import type { Step, Workflow } from "../src/app/model";

const AT = "2026-10-20T10:00:00Z";
const steps = (mapping: string): Step[] => [
  {
    id: "lookup",
    kind: "action",
    uses: "crm.lookup@1.0.0",
    with: { ref: "/input" },
  },
  {
    id: "score",
    kind: "action",
    uses: "risk.score@1.0.0",
    with: { object: { id: { ref: mapping } } },
  },
];
const workflow = (list: Step[]) =>
  ({
    spec: { steps: list, inputSchema: {}, output: { literal: null } },
  }) as unknown as Workflow;
const prints = (list: Step[]) =>
  fingerprints({
    workflow: workflow(list),
    testInput: { id: "c-1" },
    contractOf: (step) => contractOf(step, { owned: () => null, profiles: {} }),
  });

describe("pinned output and its stale marks", () => {
  it("is current when pinned, out of date after an upstream mapping changes, current when it is restored", () => {
    const before = prints(steps("/steps/lookup/output/id"));
    let data = withPin(
      emptyTestData(AT),
      "score",
      stampedPin({ tier: "review" }, "manual", before.get("score")!, AT),
      AT,
    );
    data = withPin(
      data,
      "score[1]",
      stampedPin(1, "manual", before.get("score")!, AT),
      AT,
    );
    expect(pinsView(data, before).get("score")?.stale).toBeNull();
    expect([...pinsView(data, before).keys()]).toEqual(["score"]);
    const changed = prints(steps("/steps/lookup/output/customerId"));
    expect(pinsView(data, changed).get("score")?.stale).toBe("settings");
    const restored = prints(steps("/steps/lookup/output/id"));
    expect(pinsView(data, restored).get("score")?.stale).toBeNull();
  });
  it("marks a pin out of date when what it reads changes, and Keep pinned makes it current", () => {
    const before = prints(steps("/steps/lookup/output/id"));
    const pin = stampedPin(
      { tier: "review" },
      "manual",
      before.get("score")!,
      AT,
    );
    const upstream = steps("/steps/lookup/output/id");
    upstream[0]["with"] = { ref: "/input/other" };
    const after = prints(upstream);
    const data = withPin(emptyTestData(AT), "score", pin, AT);
    expect(pinsView(data, after).get("score")?.stale).toBe("upstream");
    const kept = keepPinned(pin, after.get("score") as StepFingerprints);
    expect(kept.output).toEqual(pin.output);
    expect(
      pinsView(withPin(data, "score", kept, AT), after).get("score")?.stale,
    ).toBeNull();
    expect(staleText("upstream")).toBe("what this step reads changed");
  });
  it("reads what a step calls from its recipe, its AI profile or its version", () => {
    const owned = { recipe: 1 };
    const sources = {
      owned: (uses: string) => (uses === "mine@0.0.0-draft" ? owned : null),
      profiles: { p: { model: "m" } },
    };
    expect(
      contractOf(
        { id: "a", kind: "action", uses: "mine@0.0.0-draft" } as Step,
        sources,
      ),
    ).toBe(fingerprint(owned));
    expect(
      contractOf(
        { id: "b", kind: "action", uses: "crm.lookup@1.0.0" } as Step,
        sources,
      ),
    ).toBe("crm.lookup@1.0.0");
    expect(
      contractOf({ id: "c", kind: "llm", profile: "p" } as Step, sources),
    ).toBe(fingerprint({ model: "m" }));
    expect(contractOf({ id: "d", kind: "transform" } as Step, sources)).toBe(
      "",
    );
  });
});
