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
// Execute workflow in an environment pins each workflow the draft calls to
// its current activation there, as the activation dialog does, or says
// which one isn't active.
import { describe, expect, it } from "vitest";
import { calleePins } from "../src/app/editor/activation/execute-in-environment";

const draft = (...uses: string[]) => ({
  spec: {
    steps: uses.map((reference, index) => ({
      id: `call-${index}`,
      kind: "callWorkflow",
      uses: reference,
      with: { literal: {} },
    })),
  },
});
const versions = [
  { id: "v1", name: "notify-customer", version: "1.0.0" },
  { id: "v2", name: "billing-close", version: "2.0.0" },
];
const activation = (
  id: string,
  name: string,
  versionId: string,
  revision = 1,
) => ({
  id,
  name,
  revision,
  retired: false,
  versionId,
});

describe("pinning the workflows a draft calls", () => {
  it("pins each called version to its latest activation", () => {
    expect(
      calleePins(
        draft("notify-customer@1.0.0"),
        versions,
        [
          activation("a1", "notify-customer", "v1", 1),
          activation("a2", "notify-customer", "v1", 2),
        ],
        "local",
      ),
    ).toEqual({ pins: { "notify-customer@1.0.0": "a2" } });
  });

  it("pins nothing for a draft that calls nothing", () => {
    expect(calleePins(draft(), versions, [], "local")).toEqual({ pins: {} });
  });

  it("says which called versions aren't active in the environment", () => {
    expect(
      calleePins(draft("notify-customer@1.0.0"), versions, [], "local"),
    ).toEqual({
      refusal:
        "This workflow calls notify-customer 1.0.0, which isn't active in local. Activate it first.",
    });
    expect(
      calleePins(
        draft("notify-customer@1.0.0", "billing-close@2.0.0"),
        versions,
        // The latest activation of notify-customer runs another version.
        [activation("a1", "notify-customer", "v9")],
        "local",
      ),
    ).toEqual({
      refusal:
        "This workflow calls notify-customer 1.0.0 and billing-close 2.0.0, which aren't active in local. Activate them first.",
    });
  });
});
