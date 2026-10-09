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
  runText,
  setupPhrase,
  tileBadge,
  tileBorders,
  tileName,
  triggerSubtitle,
  type TileFacts,
} from "../src/app/editor/canvas/tile-facts";
import { freshWorkflow } from "../src/app/model";

const facts = (extra: Partial<TileFacts> = {}): TileFacts => ({
  errors: [],
  warnings: [],
  setup: [],
  run: null,
  failure: null,
  stale: null,
  pinned: false,
  unapplied: false,
  selected: false,
  ...extra,
});

describe("the corner badge", () => {
  it("shows nothing for a step with nothing to report", () => {
    expect(tileBadge(facts())).toBeNull();
  });

  it("takes the first match, from a failed run down to a finished one", () => {
    const all = facts({
      run: "failed",
      failure: { code: "WV-HTTP", message: "Timed out" },
      errors: ["Unknown field"],
      setup: ["map Customer ID"],
      warnings: ["Never read"],
      stale: "an upstream step changed",
      pinned: true,
    });
    expect(tileBadge(all)).toEqual({
      kind: "failed",
      tone: "danger",
      icons: ["critical"],
      text: "Failed: WV-HTTP, Timed out",
    });
    expect(tileBadge({ ...all, run: "done" })).toEqual({
      kind: "error",
      tone: "danger",
      icons: ["failCircle"],
      text: "Unknown field",
    });
    expect(tileBadge({ ...all, run: "done", errors: [] })).toEqual({
      kind: "setup",
      tone: "warning",
      icons: ["connections"],
      text: "Setup needed: map Customer ID",
    });
    expect(tileBadge({ ...all, run: "done", errors: [], setup: [] })).toEqual({
      kind: "warning",
      tone: "warning",
      icons: ["warning"],
      text: "Never read",
    });
    expect(tileBadge(facts({ run: "done" }))).toEqual({
      kind: "done",
      tone: "success",
      icons: ["check"],
      text: "Finished in the last run",
    });
  });

  it("puts Stale above Pin, and reads a stale pin as Pinned, stale", () => {
    expect(tileBadge(facts({ pinned: true, run: "done" }))).toEqual({
      kind: "pinned",
      tone: "info",
      icons: ["pin"],
      text: "Pinned test data",
    });
    expect(
      tileBadge(facts({ stale: "the input changed", run: "done" })),
    ).toEqual({
      kind: "stale",
      tone: "warning",
      icons: ["stale"],
      text: "Output may change when this step runs again: the input changed",
    });
    expect(
      tileBadge(facts({ stale: "the input changed", pinned: true })),
    ).toEqual({
      kind: "pinned-stale",
      tone: "warning",
      icons: ["pin", "stale"],
      text: "Pinned test data, out of date: the input changed",
    });
  });

  it("leaves the corner empty while the step runs: the live border says it", () => {
    expect(tileBadge(facts({ run: "live", errors: ["x"] }))).toBeNull();
    expect(tileBadge(facts({ run: "waiting", pinned: true }))).toBeNull();
  });

  it("reads a failure without details as Failed", () => {
    expect(tileBadge(facts({ run: "failed" }))?.text).toBe("Failed");
  });
});

describe("tile borders", () => {
  it("mark only selected, live, unapplied and error", () => {
    expect(
      tileBorders(
        facts({
          selected: true,
          unapplied: true,
          warnings: ["w"],
          setup: ["s"],
          pinned: true,
        }),
      ),
    ).toEqual({ selected: true, live: false, unapplied: true, error: false });
    expect(tileBorders(facts({ run: "waiting" }))).toMatchObject({
      live: true,
    });
    expect(tileBorders(facts({ run: "failed" }))).toMatchObject({
      error: true,
    });
    expect(tileBorders(facts({ errors: ["e"] }))).toMatchObject({
      error: true,
    });
  });
});

describe("tile words", () => {
  it("name a tile by step, kind, summary, status and place", () => {
    expect(
      tileName({
        id: "check-customer",
        kindLabel: "Call an action",
        summary: "Customer lookup",
        status: "Setup needed: map Customer ID",
        index: 1,
        count: 6,
        place: "Main sequence",
      }),
    ).toBe(
      "check-customer, Call an action, Customer lookup, Setup needed: map Customer ID, step 2 of 6 in Main sequence",
    );
    expect(
      tileName({
        id: "notify",
        kindLabel: "Transform",
        summary: "",
        status: "",
        index: 0,
        count: 2,
        place: "Amount > 1000 of route",
      }),
    ).toBe("notify, Transform, step 1 of 2 in Amount > 1000 of route");
  });

  it("say what a step waits for", () => {
    expect(runText("live", "action")).toBe("Running");
    expect(runText("waiting", "signal")).toBe("Waiting for a signal");
    expect(runText("waiting", "humanTask")).toBe("Waiting for a person");
    expect(runText("waiting", "wait")).toBe("Waiting");
    expect(runText("done", "action")).toBe("");
  });

  it("turn missing setup into a short instruction", () => {
    expect(
      [
        "Needs: Customer ID",
        "Needs a connection",
        "Choose an action",
        "Needs a compatible connection",
      ].map(setupPhrase),
    ).toEqual([
      "map Customer ID",
      "choose a connection",
      "choose an action",
      "choose a compatible connection",
    ]);
  });

  it("describe the Manual form trigger by the workflow's input fields", () => {
    const workflow = freshWorkflow();
    expect(triggerSubtitle(workflow)).toBe("Manual form");
    workflow.spec["inputSchema"] = {
      type: "object",
      properties: { amount: { type: "number" } },
    };
    expect(triggerSubtitle(workflow)).toBe("Manual form · 1 field");
    workflow.spec["inputSchema"] = {
      type: "object",
      properties: { a: {}, b: {}, c: {} },
    };
    expect(triggerSubtitle(workflow)).toBe("Manual form · 3 fields");
  });
});
