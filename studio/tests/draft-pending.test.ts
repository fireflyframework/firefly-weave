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
  DRAFT_UNPUBLISHED,
  withDraftPending,
} from "../src/app/editor/ndv/owned/draft-pending";

const definition = {
  spec: {
    steps: [
      { id: "score", kind: "decisionTable", uses: "payment-policy@1.2.1" },
      {
        id: "each",
        kind: "forEach",
        body: {
          steps: [
            { id: "check", kind: "action", uses: "onboarding.check@1.0.1" },
          ],
        },
      },
      { id: "other", kind: "decisionTable", uses: "limits@1.0.0" },
    ],
  },
};
const unknown = (path: string, code = "WV-COMP-UNKNOWN_RESOURCE") => ({
  code,
  severity: "error",
  path,
  message: "Unknown",
});

describe("a workflow that uses drafts", () => {
  it("says which draft to publish first, at its step", () => {
    const result = withDraftPending(
      {
        validationOk: false,
        errorCount: 3,
        diagnostics: [
          unknown("/spec/steps/0/uses"),
          unknown("/spec/steps/1/body/steps/0/uses", "WV-COMP-UNKNOWN_ACTION"),
          unknown("/spec/steps/2/uses"),
        ],
      },
      definition,
      new Set(["payment-policy@1.2.1", "onboarding.check@1.0.1"]),
    );
    expect(result.errorCount).toBe(3);
    expect(result.validationOk).toBe(false);
    expect(
      result.diagnostics.map((d) => [d.code, d.severity, d.message]),
    ).toEqual([
      [
        DRAFT_UNPUBLISHED,
        "error",
        "Publish payment-policy@1.2.1 before publishing this workflow.",
      ],
      [
        DRAFT_UNPUBLISHED,
        "error",
        "Publish onboarding.check@1.0.1 before publishing this workflow.",
      ],
      ["WV-COMP-UNKNOWN_RESOURCE", "error", "Unknown"],
    ]);
    expect(result.draftsPending).toBe(2);
  });

  it("adds nothing for a draft no step uses", () => {
    const result = {
      validationOk: true,
      errorCount: 0,
      diagnostics: [],
    };
    expect(
      withDraftPending(result, definition, new Set(["unused@2.0.0"])),
    ).toBe(result);
  });
});
