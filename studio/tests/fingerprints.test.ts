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
  fingerprint,
  fingerprints,
  fnv1a64,
  staleReason,
  STALE_WORDS,
  type FingerprintInputs,
} from "../src/app/editor/state/fingerprints";
import type { Step, Workflow } from "../src/app/model";

const workflow = (steps: Step[]): Workflow =>
  ({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "orders", version: "1.0.0" },
    spec: {
      inputSchema: { type: "object" },
      outputSchema: {},
      steps,
      output: { literal: null },
    },
  }) as unknown as Workflow;
const base = (): Step[] => [
  {
    id: "lookup",
    kind: "action",
    uses: "crm.lookup@1.0.0",
    with: { object: { id: { ref: "/input/customerId" } } },
  },
  {
    id: "score",
    kind: "action",
    uses: "risk.score@2.0.0",
    with: { ref: "/steps/lookup/output" },
  },
  {
    id: "route",
    kind: "switch",
    cases: [
      {
        when: {
          op: {
            name: "gt",
            args: [{ ref: "/steps/score/output/value" }, { literal: 5 }],
          },
        },
        steps: [
          {
            id: "notify",
            kind: "action",
            uses: "mail.send@1.0.0",
            with: { literal: {} },
          },
        ],
        output: { literal: {} },
      },
    ],
    default: { steps: [], output: { literal: {} } },
  },
  { id: "note", kind: "transform", value: { literal: "fixed" } },
];
const inputs = (
  steps: Step[],
  testInput: unknown = { customerId: "c-1" },
  contracts: Record<string, string> = {},
): FingerprintInputs => ({
  workflow: workflow(steps),
  testInput: testInput as FingerprintInputs["testInput"],
  contractOf: (step) => contracts[step.id] ?? String(step["uses"] ?? ""),
});

describe("fingerprints", () => {
  it("hash bytes as FNV-1a 64 does", () => {
    expect(fnv1a64("")).toBe("cbf29ce484222325");
    expect(fnv1a64("a")).toBe("af63dc4c8601ec8c");
    expect(fnv1a64("foobar")).toBe("85944171f73967e8");
    expect(fnv1a64("é😀")).toBe("52da6ebdce68376c");
    expect(fingerprint({ b: 1, a: [true, null] })).toBe(
      `fnv1a64:${fnv1a64('{"a":[true,null],"b":1}')}`,
    );
  });
  it("cover every step, nested ones too, and don't change for the same workflow", () => {
    const first = fingerprints(inputs(base()));
    expect([...first.keys()].sort()).toEqual([
      "lookup",
      "note",
      "notify",
      "route",
      "score",
    ]);
    expect(fingerprints(inputs(base()))).toEqual(first);
  });
  it("mark a step's own settings, then what it calls, then what it reads", () => {
    const before = fingerprints(inputs(base()));
    const pinOf = (id: string) => {
      const now = before.get(id)!;
      return {
        configHash: now.config,
        contractHash: now.contract,
        upstreamHash: now.upstream,
      };
    };
    const settings = base();
    settings[0]["with"] = { object: { id: { ref: "/input/email" } } };
    const changed = fingerprints(inputs(settings));
    expect(staleReason(pinOf("lookup"), changed.get("lookup"))).toBe(
      "settings",
    );
    // score reads lookup, so it is out of date once lookup changes.
    expect(staleReason(pinOf("score"), changed.get("score"))).toBe("upstream");
    // note reads nothing, so it stays current.
    expect(staleReason(pinOf("note"), changed.get("note"))).toBeNull();
    const contract = fingerprints(
      inputs(base(), undefined, { lookup: "fnv1a64:0000000000000001" }),
    );
    expect(staleReason(pinOf("lookup"), contract.get("lookup"))).toBe(
      "contract",
    );
  });
  it("follow the test input only for steps that read it", () => {
    const before = fingerprints(inputs(base(), { customerId: "c-1" }));
    const after = fingerprints(inputs(base(), { customerId: "c-2" }));
    expect(after.get("lookup")!.upstream).not.toBe(
      before.get("lookup")!.upstream,
    );
    expect(after.get("score")!.upstream).not.toBe(
      before.get("score")!.upstream,
    );
    expect(after.get("note")).toEqual(before.get("note"));
  });
  it("treat a group as its children's, without hashing them as its own settings", () => {
    const before = fingerprints(inputs(base()));
    const steps = base();
    ((steps[2]["cases"] as { steps: Step[] }[])[0].steps[0] as Step)["with"] = {
      literal: { to: "x" },
    };
    const after = fingerprints(inputs(steps));
    expect(after.get("route")!.config).toBe(before.get("route")!.config);
    expect(after.get("route")!.upstream).not.toBe(
      before.get("route")!.upstream,
    );
    expect(after.get("notify")!.config).not.toBe(before.get("notify")!.config);
  });
  it("count a missing step as nothing, and a step that is gone as changed", () => {
    const steps = base();
    steps[1]["with"] = { ref: "/steps/gone/output" };
    expect(fingerprints(inputs(steps)).get("score")).toBeDefined();
    const pin = {
      configHash: "fnv1a64:0",
      contractHash: "fnv1a64:0",
      upstreamHash: "fnv1a64:0",
    };
    expect(staleReason(pin as never, undefined)).toBe("settings");
  });
  it("hash 200 steps quickly", () => {
    const many: Step[] = Array.from({ length: 200 }, (_, index) => ({
      id: `step-${index}`,
      kind: "action",
      uses: "crm.lookup@1.0.0",
      with: {
        object: {
          id: index
            ? { ref: `/steps/step-${index - 1}/output/id` }
            : { ref: "/input/id" },
          note: { literal: "x".repeat(200) },
        },
      },
    }));
    fingerprints(inputs(many));
    const started = performance.now();
    for (let round = 0; round < 5; round++) fingerprints(inputs(many));
    // The browser gate (10 ms) runs in the performance suite; here a generous bound catches a quadratic slip.
    expect((performance.now() - started) / 5).toBeLessThan(50);
  });
});

describe("fingerprints of steps inside a loop", () => {
  const loop = (
    fetchWith: unknown,
    items: unknown,
    reads: unknown = { ref: "/item" },
  ): Step[] =>
    [
      { id: "fetch", kind: "action", uses: "crm.list@1.0.0", with: fetchWith },
      {
        id: "each",
        kind: "forEach",
        items,
        concurrency: 1,
        body: {
          steps: [
            {
              id: "send",
              kind: "action",
              uses: "mail.send@1.0.0",
              with: reads,
            },
          ],
          output: { literal: null },
        },
      },
    ] as unknown as Step[];
  const stale = (
    before: Step[],
    after: Step[],
    id: string,
    inputs_: [unknown, unknown] = [{ orders: [1] }, { orders: [1] }],
  ) => {
    const was = fingerprints(inputs(before, inputs_[0])).get(id)!;
    return staleReason(
      {
        configHash: was.config,
        contractHash: was.contract,
        upstreamHash: was.upstream,
      },
      fingerprints(inputs(after, inputs_[1])).get(id),
    );
  };
  const items = { ref: "/steps/fetch/output" };
  it("go out of date when the step the loop goes over changes", () => {
    expect(
      stale(
        loop({ literal: {} }, items),
        loop({ literal: { all: true } }, items),
        "send",
      ),
    ).toBe("upstream");
  });
  it("go out of date when the loop's items change", () => {
    expect(
      stale(
        loop({ literal: {} }, items),
        loop({ literal: {} }, { ref: "/steps/fetch/output/other" }),
        "send",
      ),
    ).toBe("upstream");
  });
  it("go out of date when the loop goes over the test input and it changes", () => {
    const steps = loop({ literal: {} }, { ref: "/input/orders" });
    expect(
      stale(steps, steps, "send", [{ orders: [1] }, { orders: [2] }]),
    ).toBe("upstream");
  });
  it("follow a loop named by its ID, and stay current when nothing it reads changes", () => {
    const named = { ref: "/loops/each/index" };
    expect(
      stale(
        loop({ literal: {} }, items, named),
        loop({ literal: { all: true } }, items, named),
        "send",
      ),
    ).toBe("upstream");
    const constant = { literal: 1 };
    expect(
      stale(
        loop({ literal: {} }, items, constant),
        loop({ literal: { all: true } }, items, constant),
        "send",
      ),
    ).toBeNull();
  });
});

describe("fingerprints hold to what a step is", () => {
  const pinOf = (
    all: Map<string, { config: string; contract: string; upstream: string }>,
    id: string,
  ) => {
    const now = all.get(id)!;
    return {
      configHash: now.config,
      contractHash: now.contract,
      upstreamHash: now.upstream,
    };
  };
  const groups = (send: unknown): Step[] =>
    [
      {
        id: "fan",
        kind: "parallel",
        branches: {
          a: {
            steps: [
              {
                id: "left",
                kind: "action",
                uses: "mail.send@1.0.0",
                with: send,
              },
            ],
            output: { literal: null },
          },
          b: { steps: [], output: { literal: null } },
        },
      },
      {
        id: "each",
        kind: "forEach",
        items: { literal: [1] },
        concurrency: 1,
        body: {
          steps: [
            {
              id: "inner",
              kind: "action",
              uses: "mail.send@1.0.0",
              with: send,
            },
          ],
          output: { literal: null },
        },
      },
    ] as unknown as Step[];
  it("keep a parallel step's and a loop's settings apart from their children's", () => {
    const before = fingerprints(inputs(groups({ literal: 1 })));
    const after = fingerprints(inputs(groups({ literal: 2 })));
    for (const id of ["fan", "each"]) {
      expect(after.get(id)!.config, id).toBe(before.get(id)!.config);
      expect(staleReason(pinOf(before, id), after.get(id)), id).toBe(
        "upstream",
      );
    }
  });
  it("don't depend on the order of keys in what a step reads", () => {
    const reads = (first: string, second: string): Step[] =>
      [
        { id: "a", kind: "transform", value: { literal: 1 } },
        { id: "b", kind: "transform", value: { literal: 2 } },
        {
          id: "c",
          kind: "transform",
          value: {
            object: Object.fromEntries([
              [first, { ref: `/steps/${first === "x" ? "a" : "b"}/output` }],
              [second, { ref: `/steps/${second === "x" ? "a" : "b"}/output` }],
            ]),
          },
        },
      ] as unknown as Step[];
    expect(fingerprints(inputs(reads("x", "y"))).get("c")).toEqual(
      fingerprints(inputs(reads("y", "x"))).get("c"),
    );
  });
  it("mark a step out of date when what an earlier step calls changes", () => {
    const before = fingerprints(inputs(base()));
    const after = fingerprints(
      inputs(base(), undefined, { lookup: "crm.lookup@2.0.0" }),
    );
    expect(staleReason(pinOf(before, "score"), after.get("score"))).toBe(
      "upstream",
    );
  });
  it("read escaped step IDs in references", () => {
    const steps = (value: number): Step[] =>
      [
        { id: "a/b~c", kind: "transform", value: { literal: value } },
        {
          id: "read",
          kind: "transform",
          value: { ref: "/steps/a~1b~0c/output" },
        },
      ] as unknown as Step[];
    const before = fingerprints(inputs(steps(1)));
    expect(
      staleReason(
        pinOf(before, "read"),
        fingerprints(inputs(steps(2))).get("read"),
      ),
    ).toBe("upstream");
  });
});

describe("renaming a step", () => {
  const pinOf = (
    all: Map<string, { config: string; contract: string; upstream: string }>,
    id: string,
  ) => {
    const now = all.get(id)!;
    return {
      configHash: now.config,
      contractHash: now.contract,
      upstreamHash: now.upstream,
    };
  };
  const renamed = (): Step[] => {
    const steps = base();
    steps[0]["id"] = "find";
    steps[1]["with"] = { ref: "/steps/find/output" };
    return steps;
  };
  it("keeps its own pin and the pins of the steps that read it current", () => {
    const before = fingerprints(inputs(base()));
    const after = fingerprints(inputs(renamed()));
    expect(staleReason(pinOf(before, "lookup"), after.get("find"))).toBeNull();
    expect(staleReason(pinOf(before, "score"), after.get("score"))).toBeNull();
    expect(staleReason(pinOf(before, "route"), after.get("route"))).toBeNull();
  });
  it("keeps a group current when one of its steps is renamed", () => {
    const steps = base();
    ((steps[2]["cases"] as { steps: Step[] }[])[0].steps[0] as Step)["id"] =
      "alert";
    const before = fingerprints(inputs(base()));
    expect(
      staleReason(
        pinOf(before, "route"),
        fingerprints(inputs(steps)).get("route"),
      ),
    ).toBeNull();
  });
  it("says what this step reads changed when it reads another step instead", () => {
    const steps = base();
    steps[1]["with"] = { ref: "/steps/note/output" };
    const before = fingerprints(inputs(base()));
    expect(
      staleReason(
        pinOf(before, "score"),
        fingerprints(inputs(steps)).get("score"),
      ),
    ).toBe("upstream");
    expect(STALE_WORDS.upstream).toBe("what this step reads changed");
  });
  it("finds references inside an object field named literal", () => {
    const steps = (value: number): Step[] =>
      [
        { id: "a", kind: "transform", value: { literal: value } },
        {
          id: "b",
          kind: "transform",
          value: { object: { literal: { ref: "/steps/a/output" } } },
        },
      ] as unknown as Step[];
    const before = fingerprints(inputs(steps(1)));
    expect(
      staleReason(pinOf(before, "b"), fingerprints(inputs(steps(2))).get("b")),
    ).toBe("upstream");
  });
});

describe("fingerprints in nested loops", () => {
  const nested = (
    fetchWith: unknown,
    bodyReads: unknown,
    innerItems: unknown = { ref: "/item/lines" },
  ): Step[] =>
    [
      { id: "fetch", kind: "action", uses: "crm.list@1.0.0", with: fetchWith },
      {
        id: "outer",
        kind: "forEach",
        items: { ref: "/steps/fetch/output" },
        concurrency: 1,
        body: {
          steps: [
            {
              id: "inner",
              kind: "forEach",
              items: innerItems,
              concurrency: 1,
              body: {
                steps: [
                  {
                    id: "send",
                    kind: "action",
                    uses: "mail.send@1.0.0",
                    with: bodyReads,
                  },
                ],
                output: { literal: null },
              },
            },
          ],
          output: { literal: null },
        },
      },
    ] as unknown as Step[];
  const stale = (before: Step[], after: Step[]) => {
    const was = fingerprints(inputs(before)).get("send")!;
    return staleReason(
      {
        configHash: was.config,
        contractHash: was.contract,
        upstreamHash: was.upstream,
      },
      fingerprints(inputs(after)).get("send"),
    );
  };
  it("follow what the outer loop goes over through the inner loop's items", () => {
    for (const reads of [{ ref: "/item" }, { ref: "/loops/outer/item" }])
      expect(
        stale(
          nested({ literal: {} }, reads),
          nested({ literal: { all: true } }, reads),
        ),
        JSON.stringify(reads),
      ).toBe("upstream");
  });
  it("bind /item to the innermost loop", () => {
    const reads = { ref: "/item" };
    expect(
      stale(
        nested({ literal: {} }, reads),
        nested({ literal: {} }, reads, { ref: "/item/other" }),
      ),
    ).toBe("upstream");
  });
});
