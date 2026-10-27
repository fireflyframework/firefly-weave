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
// What Execute asks before it runs anything.
import { describe, expect, it } from "vitest";
import { executeGate } from "../src/app/editor/state/execute-gate";

describe("before executing", () => {
  it("asks to fix errors in what runs, then to unpin a pinned target", () => {
    const issues = [
      { stepId: "check", severity: "error" as const },
      { stepId: "later", severity: "error" as const },
      { stepId: "send", severity: "warning" as const },
    ];
    expect(executeGate(issues, ["check", "send"], true)).toEqual({
      kind: "fix",
      title: "Fix 1 issue first",
      issues: [issues[0]],
    });
    expect(executeGate(issues.slice(2), ["check", "send"], true)).toEqual({
      kind: "unpin",
      title: "Unpin and execute?",
      stepId: "send",
    });
    expect(executeGate(issues.slice(2), ["check", "send"], false)).toEqual({
      kind: "go",
    });
  });
  it("counts issues in plain words", () => {
    const issues = [
      { stepId: "check", severity: "error" as const },
      { stepId: "send", severity: "error" as const },
    ];
    expect(executeGate(issues, ["check", "send"], false)).toMatchObject({
      kind: "fix",
      title: "Fix 2 issues first",
    });
  });
});
