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
import { contractGaps } from "../src/app/designer/contract-gaps";
import type { Step, Workflow } from "../src/app/model";
const workflow = {
  spec: { steps: [], connections: { crm: { connector: "crm@1.0.0" } } },
} as unknown as Workflow;
const contract = {
  spec: {
    connection: { connector: "crm@1.0.0" },
    inputSchema: {
      type: "object",
      required: ["customer"],
      properties: {
        customer: { type: "string", title: "Customer" },
        optional: { type: "boolean" },
      },
    },
  },
};
describe("contract gaps on the canvas", () => {
  it("reports required data and a missing connection with exact focus paths", () => {
    const gaps = contractGaps(
      { id: "call", kind: "action", with: { literal: {} } } as Step,
      contract,
      workflow,
    );
    expect(gaps.map((gap) => gap.label)).toEqual([
      "Needs: Customer",
      "Needs a connection",
    ]);
    expect(gaps[0].dataPath).toEqual(["customer"]);
    expect(gaps[1].field).toBe("connection");
  });
  it("accepts references, false values and a compatible slot, but reports absent or wrong slots", () => {
    const step = {
      id: "call",
      kind: "action",
      connection: "crm",
      with: {
        object: {
          customer: { ref: "/input/id" },
          optional: { literal: false },
        },
      },
    } as Step;
    expect(contractGaps(step, contract, workflow)).toEqual([]);
    expect(
      contractGaps({ ...step, connection: "unknown" }, contract, workflow)[0]
        .label,
    ).toBe("Needs a connection");
    expect(
      contractGaps(
        step,
        {
          spec: { ...contract.spec, connection: { connector: "other@1.0.0" } },
        },
        workflow,
      )[0].label,
    ).toBe("Needs a compatible connection");
  });
  it("finds nested required inputs and leaves whole-reference expressions to project validation", () => {
    const nested = {
      spec: {
        inputSchema: {
          type: "object",
          required: ["parameters"],
          properties: {
            parameters: {
              type: "object",
              title: "Parameters",
              required: ["id"],
              properties: { id: { type: "string", title: "Customer ID" } },
            },
          },
        },
      },
    };
    expect(
      contractGaps(
        {
          id: "call",
          kind: "action",
          with: { object: { parameters: { object: {} } } },
        } as Step,
        nested,
        workflow,
      )[0],
    ).toMatchObject({
      label: "Needs: Parameters › Customer ID",
      dataPath: ["parameters", "id"],
    });
    expect(
      contractGaps(
        { id: "call", kind: "action", with: { ref: "/input" } } as Step,
        nested,
        workflow,
      ),
    ).toEqual([]);
  });
});
