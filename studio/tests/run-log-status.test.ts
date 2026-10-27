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
// The run log's status line: short enough for one line, names kept whole.
import "@angular/compiler";
import { describe, expect, it } from "vitest";
import { runStatus } from "../src/app/editor/debugger/run-log-panel";
import { RESPONSE } from "./execution-fixtures";

describe("the run log's status", () => {
  it("says what a waiting run waits for, in a few words", () => {
    const blocked = (reason: string, nodeId: string, message: string) =>
      runStatus(
        {
          ...RESPONSE,
          status: "blocked",
          blocked: { reason, nodeId, instanceKey: "", message },
        } as never,
        null,
      );
    expect(blocked("signal", "paid", "Send signal order-paid")).toBe(
      "Waiting for order-paid",
    );
    expect(blocked("human", "approval", "Decide for approval")).toBe(
      "Waiting for approval",
    );
    expect(
      blocked("missing_mock", "lookup", "Step lookup needs test data."),
    ).toBe("lookup needs test data");
    expect(
      blocked("path", "notify", "With this test data, route takes path 2."),
    ).toBe("Stopped at notify");
  });
});
