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
// Inside a step's error path the Input pane's first source is that step's
// error, "Error of submit", with its fields; its data is the failure
// simulated for the step, as the platform reports it.
import { describe, expect, it } from "vitest";
import {
  defaultSource,
  errorData,
  inputSources,
  sourceRows,
} from "../src/app/editor/ndv/panes/views";
import { referenceScope } from "../src/app/forms/core/scope";
import { freshWorkflow, type Step } from "../src/app/model";

const transform = (id: string, ref = "/input"): Step => ({
  id,
  kind: "transform",
  value: { ref },
});
function workflow() {
  const document = freshWorkflow();
  document.spec.steps = [
    transform("lookup"),
    {
      id: "submit",
      kind: "transform",
      value: { literal: 1 },
      onError: "errorOutput",
      errorPath: {
        steps: [transform("notify-team", "/steps/submit/error/code")],
        output: { literal: null },
      },
    },
    transform("after"),
  ];
  return document;
}

describe("the Input pane inside an error path", () => {
  it("starts on the failed step's error, with its fields", () => {
    const scope = referenceScope(workflow(), "notify-team", "/value");
    const sources = inputSources(scope);
    expect(sources.map((source) => [source.id, source.label])).toEqual([
      ["error:submit", "Error of submit"],
      ["step:lookup", "lookup"],
      ["input", "Workflow input"],
    ]);
    expect(defaultSource(sources)).toBe("error:submit");
    expect(
      sourceRows(scope.entries, "error:submit").map((row) => row.ref),
    ).toEqual(
      expect.arrayContaining([
        "/steps/submit/error/code",
        "/steps/submit/error/message",
      ]),
    );
  });

  it("lists no error source after the step, where its error may be missing", () => {
    const scope = referenceScope(workflow(), "after", "/value");
    expect(inputSources(scope).map((source) => source.id)).not.toContain(
      "error:submit",
    );
  });

  it("reads the simulated failure as the platform reports the error", () => {
    const fetch: Step = { id: "fetch", kind: "action" };
    expect(errorData({ code: "WV-EXPR-MISSING" }, fetch, "node:fetch")).toEqual(
      {
        code: "WV-EXPR-MISSING",
        message: "A value the step reads is missing.",
        outcome: "failed",
        attempts: 1,
        instanceKey: "node:fetch",
      },
    );
    expect(
      errorData(
        { code: "REQUEST_REFUSED", outcome: "unknown", attempts: 3 },
        fetch,
        "node:fetch",
      ),
    ).toEqual({
      code: "REQUEST_REFUSED",
      message: "The step failed with code REQUEST_REFUSED.",
      outcome: "unknown",
      attempts: 3,
      instanceKey: "node:fetch",
    });
  });

  it("reports a transform's or a decision table's failure as not started, as the platform does", () => {
    for (const kind of ["transform", "decisionTable"])
      for (const outcome of [undefined, "failed"] as const)
        expect(
          errorData(
            { code: "WV-EXPR-MISSING", ...(outcome ? { outcome } : {}) },
            { id: "submit", kind } as Step,
            "node:submit",
          ),
          `${kind} with ${outcome ?? "no"} outcome`,
        ).toMatchObject({ outcome: "not_started" });
  });
});
