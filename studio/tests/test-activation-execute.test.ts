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
// Execute workflow in <environment>: why it can't run now, the question it
// asks before steps that change data, and the connections it binds.
import { describe, expect, it } from "vitest";
import {
  sideEffectQuestion,
  workflowTestBindings,
  workflowTestReason,
} from "../src/app/editor/activation/execute-in-environment";

const on = { test_calls: "enabled" as const, revision: 1 };

describe("Execute workflow in an environment", () => {
  it("says why it can't run: read only, not saved, the policy unread, unreadable or off", () => {
    const state = {
      environment: "local",
      policy: on,
      readOnly: null,
      unsaved: false,
    };
    expect(workflowTestReason(state)).toBeNull();
    expect(
      workflowTestReason({ ...state, readOnly: "This version is read only." }),
    ).toBe("This version is read only.");
    expect(workflowTestReason({ ...state, unsaved: true })).toBe(
      "Save this workflow to the platform to execute it in local.",
    );
    expect(workflowTestReason({ ...state, policy: null })).toBe(
      "Studio is checking whether test calls are on in local.",
    );
    expect(workflowTestReason({ ...state, policy: "unreadable" })).toBe(
      "Your account can't see whether test calls are on in local.",
    );
    expect(
      workflowTestReason({
        ...state,
        policy: { test_calls: "disabled", revision: 2 },
      }),
    ).toBe(
      "Test calls are off in local. Choose an environment where they're on to execute the workflow there.",
    );
  });

  it("names the steps that change data before it runs them", () => {
    expect(sideEffectQuestion(["charge"], "local")).toEqual({
      title: "Execute steps that change data?",
      message:
        "charge changes data in local. Executing the workflow repeats the change.",
      confirmLabel: "Run it",
      cancelLabel: "Cancel",
      danger: true,
    });
    expect(
      sideEffectQuestion(["charge", "notify", "refund"], "local").message,
    ).toBe(
      "charge, notify and refund change data in local. Executing the workflow repeats the changes.",
    );
    expect(sideEffectQuestion([], "local").message).toBe(
      "This workflow changes data in local. Executing it repeats the change.",
    );
  });

  it("binds each slot to the connection this computer bound it to, and leaves the rest out", () => {
    const binding = (slot: string) =>
      slot === "crm" ? { revisionId: "rev-1" } : null;
    expect(
      workflowTestBindings(
        [
          { name: "crm", connector: "weave-http@2.0.0", required: true },
          { name: "mail", connector: "weave-email@1.0.0", required: false },
        ],
        binding,
      ),
    ).toEqual({ connection_revision_ids: { crm: "rev-1" } });
    expect(workflowTestBindings([], binding)).toEqual({});
  });
});
