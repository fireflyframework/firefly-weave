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
// What a loop's frame says in its header: the list it goes over, printed as
// a formula, and how many items run at a time when that's more than one.
import { describe, expect, it } from "vitest";
import {
  drawnSelected,
  endsAtFrame,
  frameBadge,
  frameHeaderHeight,
  loopFacts,
  loopHeader,
  loopHeaderParts,
  loopRunText,
} from "../src/app/editor/canvas/loop-frame";
import { createStep, type Step } from "../src/app/model";

const loop = (extra: Record<string, unknown>): Step => ({
  ...createStep("forEach", "orders"),
  ...extra,
});

describe("a loop's header", () => {
  it("names the list and how many items run at a time", () => {
    expect(
      loopHeader(loop({ items: { ref: "/input/orders" }, concurrency: 4 })),
    ).toBe("For each item in input.orders · up to 4 at a time");
  });

  it("leaves out the items at a time when they run one by one", () => {
    expect(loopHeader(loop({ items: { ref: "/input/orders" } }))).toBe(
      "For each item in input.orders",
    );
    expect(
      loopHeader(loop({ items: { ref: "/input/orders" }, concurrency: 1 })),
    ).toBe("For each item in input.orders");
  });

  it("waits for the list while none is chosen", () => {
    expect(loopHeader(loop({}))).toBe("For each item in …");
    expect(loopHeader(loop({ concurrency: 4 }))).toBe(
      "For each item in … · up to 4 at a time",
    );
  });

  it("prints the list as the formula editor writes it", () => {
    expect(loopHeader(loop({ items: { ref: "/item/lines" } }))).toBe(
      "For each item in item.lines",
    );
    expect(
      loopHeader(loop({ items: { ref: "/steps/fetch/output/open orders" } })),
    ).toBe('For each item in steps.fetch.output["open orders"]');
    // A tree outside the language reads as not chosen, never as raw JSON.
    expect(loopHeader(loop({ items: { script: "orders()" } }))).toBe(
      "For each item in …",
    );
  });
});

describe("a loop's header, part by part", () => {
  it("keeps the list and the items at a time apart, so the header can shorten each", () => {
    expect(
      loopHeaderParts(
        loop({ items: { ref: "/input/orders" }, concurrency: 4 }),
      ),
    ).toEqual({ items: "input.orders", suffix: "· up to 4 at a time" });
    expect(loopHeaderParts(loop({ concurrency: 1 }))).toEqual({
      items: null,
      suffix: "",
    });
  });
  it("treats a list that prints as nothing as not chosen", () => {
    expect(
      loopHeaderParts(loop({ items: { ref: "/input/orders" } }), () => "")
        .items,
    ).toBeNull();
  });
});

describe("the header band's drawn height", () => {
  it("is 32 units from 75% up and keeps 24 px on screen below, within the band and its gap", () => {
    expect(frameHeaderHeight(2)).toBe(32);
    expect(frameHeaderHeight(1)).toBe(32);
    expect(frameHeaderHeight(0.75)).toBe(32);
    expect(frameHeaderHeight(0.6)).toBe(40);
    // Never over the body's tiles: the band and the 24 units under it.
    expect(frameHeaderHeight(0.3)).toBe(56);
  });
});

describe("edges at a frame's sides", () => {
  const loops = new Set(["orders"]);
  it("end at the frame when they go into a loop or out of its body", () => {
    expect(endsAtFrame({ key: "fetch>orders", enters: "orders" }, loops)).toBe(
      true,
    );
    expect(
      endsAtFrame({ key: "$trigger:form>orders", enters: "orders" }, loops),
    ).toBe(true);
    expect(endsAtFrame({ key: "send>$exit:orders", enters: null }, loops)).toBe(
      true,
    );
    expect(
      endsAtFrame(
        { key: "$slot:orders/body>$exit:orders", enters: null },
        loops,
      ),
    ).toBe(true);
  });
  it("end at a step or an empty body's slot otherwise", () => {
    expect(endsAtFrame({ key: "orders>check", enters: "check" }, loops)).toBe(
      false,
    );
    expect(
      endsAtFrame({ key: "orders>summarize", enters: "summarize" }, loops),
    ).toBe(false);
    expect(
      endsAtFrame({ key: "orders>$slot:orders/body", enters: null }, loops),
    ).toBe(false);
    expect(
      endsAtFrame({ key: "fetch>orders", enters: "orders" }, new Set()),
    ).toBe(false);
  });
});

describe("the steps drawn selected", () => {
  // orders holds check and route; route (a decision) holds lines (a loop) and
  // skip; lines holds reserve.
  const parents: Record<string, string | null> = {
    fetch: null,
    orders: null,
    check: "orders",
    route: "orders",
    lines: "route",
    skip: "route",
    reserve: "lines",
  };
  const parentOf = (id: string) => parents[id] ?? null;
  const loops = new Set(["orders", "lines"]);
  const all = new Set(Object.keys(parents));
  const inside = (id: string) =>
    new Set(
      [...all].filter((item) => {
        for (let at: string | null = item; at; at = parentOf(at))
          if (at === id) return true;
        return false;
      }),
    );

  it("are a selected loop alone: its frame says the steps it repeats come with it", () => {
    expect(
      drawnSelected(inside("orders"), ["orders"], parentOf, loops),
    ).toEqual(new Set(["orders"]));
  });
  it("are a selected decision with its paths, up to a loop's frame inside them", () => {
    expect(drawnSelected(inside("route"), ["route"], parentOf, loops)).toEqual(
      new Set(["route", "lines", "skip"]),
    );
  });
  it("include a step selected inside a loop, and steps outside loops", () => {
    expect(
      drawnSelected(
        new Set(["fetch", "check"]),
        ["fetch", "check"],
        parentOf,
        loops,
      ),
    ).toEqual(new Set(["fetch", "check"]));
  });
});

describe("a loop's run status in its header", () => {
  it("says how many items finished of how many, or the item it failed at", () => {
    const run = (extra: Record<string, unknown>) => ({
      finished: 7,
      count: 20,
      status: "running" as const,
      failedAt: null,
      ...extra,
    });
    expect(loopRunText(run({}))).toBe("7 of 20");
    expect(loopRunText(run({ finished: 20, status: "done" }))).toBe("20 of 20");
    expect(loopRunText(run({ status: "failed", failedAt: 4 }))).toBe(
      "failed at item 4",
    );
    // Without a count, how many finished.
    expect(loopRunText(run({ count: null }))).toBe("7 finished");
    expect(loopRunText(run({ count: 1200, finished: 1000 }))).toBe(
      "1,000 of 1,200",
    );
  });
});

describe("the badge at a loop's header", () => {
  const facts = { errors: [], warnings: [], setup: [] };
  it("says a loop without a list needs one, as setup", () => {
    expect(loopFacts(loop({}), facts).setup).toEqual([
      "choose the list to loop over",
    ]);
    expect(
      loopFacts(loop({ items: { ref: "/input/orders" } }), facts).setup,
    ).toEqual([]);
    // Said once, whatever else the step needs.
    expect(
      loopFacts(loop({}), { ...facts, setup: ["choose the list to loop over"] })
        .setup,
    ).toEqual(["choose the list to loop over"]);
  });

  it("keeps what the loop needs and leaves run states to the steps inside", () => {
    const badge = (kind: string) => ({
      kind,
      tone: "info" as const,
      icons: [],
      text: kind,
    });
    for (const kept of ["setup", "error", "warning"])
      expect(frameBadge(badge(kept))?.kind).toBe(kept);
    for (const dropped of ["failed", "done", "changed", "added", "pinned"])
      expect(frameBadge(badge(dropped))).toBeNull();
    expect(frameBadge(null)).toBeNull();
  });
});
