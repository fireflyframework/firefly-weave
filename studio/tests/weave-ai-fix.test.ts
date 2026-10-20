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
  fixRequest,
  problemLabel,
  type LumiProposal,
} from "../src/app/lumi/lumi-state";

const proposal: LumiProposal = {
  title: "Greet",
  kind: "workflow",
  format: "yaml",
  source: "kind: Workflow\n",
  diagnostics: 2,
};

describe("asking Weave AI to fix a proposal", () => {
  it("sends the proposal as the draft and lists what the platform found", () => {
    expect(
      fixRequest(proposal, [
        "Unknown reference /steps/missing/output.",
        "The output schema is required.",
      ]),
    ).toEqual({
      message:
        'Fix the proposal "Greet". The platform found 2 problems:\n- Unknown reference /steps/missing/output.\n- The output schema is required.',
      draft: { format: "yaml", source: "kind: Workflow\n" },
    });
  });
  it("stays within the message limit", () => {
    const { message } = fixRequest(proposal, ["x".repeat(30000)]);
    expect(message.length).toBeLessThanOrEqual(20000);
    expect(message.endsWith("…")).toBe(true);
  });
  it("names the problem count on the proposal", () => {
    expect(problemLabel(proposal)).toBe("2 problems");
    expect(problemLabel({ ...proposal, diagnostics: 1 })).toBe("1 problem");
    expect(problemLabel({ ...proposal, diagnostics: 0 })).toBe("");
    expect(problemLabel({ ...proposal, diagnostics: undefined })).toBe("");
  });
});
