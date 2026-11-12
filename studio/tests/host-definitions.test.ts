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
  TOO_MANY_DRAFTS,
  hostDefinitions,
  usesOf,
} from "../src/app/editor/state/host-definitions";

const doc = (kind: string, name: string, version = "1.0.0", pad = 0) => ({
  apiVersion: "weave/v1alpha1",
  kind,
  metadata: { name, version },
  spec: { pad: "x".repeat(pad) },
});
const workflow = {
  spec: {
    steps: [
      { id: "a", kind: "action", uses: "lookup@1.0.0" },
      {
        id: "p",
        kind: "parallel",
        branches: {
          left: {
            steps: [{ id: "t", kind: "decisionTable", uses: "policy@1.2.1" }],
          },
        },
      },
      {
        id: "each",
        kind: "forEach",
        body: {
          steps: [{ id: "u", kind: "decisionTable", uses: "limits@1.0.0" }],
        },
      },
    ],
  },
};

describe("what Studio sends its host", () => {
  it("finds the uses of every step, nested ones too", () => {
    expect(usesOf(workflow)).toEqual([
      "lookup@1.0.0",
      "policy@1.2.1",
      "limits@1.0.0",
    ]);
  });

  it("sends owned documents first, then action drafts, then tables, each once", async () => {
    const asked: string[][] = [];
    const found = await hostDefinitions(workflow, {
      owned: [doc("Action", "lookup"), doc("Action", "lookup")],
      actions: async (uses) => {
        asked.push(uses);
        return [];
      },
      tables: async (uses) => {
        asked.push(uses);
        return [
          doc("DecisionTable", "policy", "1.2.1"),
          doc("DecisionTable", "limits"),
        ];
      },
    });
    expect(
      found.map(
        (d) => `${d["kind"]}:${(d["metadata"] as { name: string }).name}`,
      ),
    ).toEqual([
      "Action:lookup",
      "DecisionTable:policy",
      "DecisionTable:limits",
    ]);
    expect(asked).toEqual([
      ["lookup@1.0.0", "policy@1.2.1", "limits@1.0.0"],
      ["lookup@1.0.0", "policy@1.2.1", "limits@1.0.0"],
    ]);
  });

  it("says when the drafts are more than the host takes at once", async () => {
    await expect(
      hostDefinitions(workflow, {
        owned: [],
        actions: async () => [],
        tables: async () => [
          doc("DecisionTable", "policy", "1.2.1", 2_200_000),
        ],
      }),
    ).rejects.toThrow(TOO_MANY_DRAFTS);
  });
});
