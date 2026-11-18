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
// The subtitle under a step's name never shows a connection slot's name,
// which reads like a protocol ("http"): it names the connection chosen for
// that slot, or nothing.
import "@angular/compiler";
import { describe, expect, it } from "vitest";
import { headerSubtitle } from "../src/app/editor/ndv/header";
import type { KindContext } from "../src/app/editor/ndv/registry";
import { createStep, freshWorkflow } from "../src/app/model";

const ctx = {
  workflow: freshWorkflow(),
  features: [],
  actionContract: () => null,
  tableContract: () => null,
  workflowContract: () => null,
} as KindContext;

describe("a step's subtitle and its connection", () => {
  const step = {
    ...createStep("action", "crm-lookup"),
    uses: "acme.crm-lookup@1.0.0",
    connection: "http",
  };

  it("names the connection chosen for the slot", () => {
    expect(
      headerSubtitle(step, ctx, "Call an action", (slot) =>
        slot === "http" ? "acme-crm-1" : null,
      ),
    ).toBe("Call an action · acme.crm-lookup@1.0.0 · acme-crm-1");
  });

  it("shows no slot name when none is chosen", () => {
    expect(headerSubtitle(step, ctx, "Call an action")).toBe(
      "Call an action · acme.crm-lookup@1.0.0",
    );
  });
});
