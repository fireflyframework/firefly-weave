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
// A loop on the canvas is a frame around its body. This module holds what
// the frame says and its sizes; the layout places frames, the canvas draws
// them. Pure, and only loaded with the editor's canvas.
import type { Step } from "../../model";
import { printFormula } from "../formula/printer";
import type { StepFacts, TileBadge } from "./tile-facts";

/** Frame sizes in canvas units. */
export const FRAME = {
  /** The header band: the loop's icon, name and list. */
  header: 32,
  /** Under the header, above the body's row. */
  top: 24,
  /**
   * Left and right of the body: room for the inside half of the entry edge
   * (where "Insert a step at the start" is hovered) and for the "+" after
   * the last step, 40 units out.
   */
  side: 64,
  /** Under a step's tile: its label gap and three label lines at 60%. */
  labels: 88,
  /** Under the labels of the body's lowest step, or a nested frame's bottom. */
  bottom: 24,
  /** The narrowest frame, so a just-added loop's header fits whole at 60%. */
  minWidth: 360,
  /** Under a frame: its own label line (the loop's result once it ran) and a gap. */
  after: 48,
} as const;

/**
 * The header's parts: the list as a formula prints it (null until it is
 * chosen) and "· up to 4 at a time" when more than one item runs at a time.
 */
export function loopHeaderParts(
  step: Step,
  print: (expression: unknown) => string | null = printFormula,
): { items: string | null; suffix: string } {
  const concurrency = step["concurrency"];
  return {
    items: step["items"] === undefined ? null : print(step["items"]) || null,
    suffix:
      typeof concurrency === "number" && concurrency > 1
        ? `· up to ${concurrency} at a time`
        : "",
  };
}

/** "For each item in input.orders · up to 4 at a time"; "…" until the list is chosen. */
export function loopHeader(
  step: Step,
  print: (expression: unknown) => string | null = printFormula,
): string {
  const { items, suffix } = loopHeaderParts(step, print);
  return `For each item in ${items ?? "…"}${suffix ? ` ${suffix}` : ""}`;
}

/**
 * The header band's height as drawn, in canvas units: the 32-unit band,
 * kept at least 24 px on screen as the canvas zooms out, and never over the
 * body's steps 24 units under it.
 */
export function frameHeaderHeight(zoom: number): number {
  return Math.min(FRAME.header + FRAME.top, Math.max(FRAME.header, 24 / zoom));
}

/**
 * An edge that ends at a frame's side: into a loop (it goes on to the
 * body's first step) or out of the body (it goes on to the step after).
 * Arrowheads point at steps, never at a frame.
 */
export function endsAtFrame(
  edge: { key: string; enters: string | null },
  loops: ReadonlySet<string>,
): boolean {
  return (
    (edge.enters !== null && loops.has(edge.enters)) ||
    edge.key.slice(edge.key.lastIndexOf(">") + 1).startsWith("$exit:")
  );
}

/**
 * The steps drawn selected, of those a selection covers: a selected loop's
 * frame says the steps it repeats come with it, so they draw as they are;
 * the steps of a selected decision or parallel step draw selected.
 */
export function drawnSelected(
  covered: ReadonlySet<string>,
  selected: readonly string[],
  parentOf: (id: string) => string | null,
  loops: ReadonlySet<string>,
): Set<string> {
  const own = new Set(selected);
  const throughLoop = (id: string): boolean => {
    for (let parent = parentOf(id); parent; parent = parentOf(parent)) {
      if (loops.has(parent)) return true;
      if (own.has(parent)) return false;
    }
    return false;
  };
  return new Set([...covered].filter((id) => own.has(id) || !throughLoop(id)));
}

/**
 * What a loop's run gave, under its frame: "20 items", or "20 items, 2
 * skipped" when Skip empty results left some out ("No items" for an empty
 * list). `results` is how many the loop returned; null when not known.
 */
export function loopResultText(count: number, results: number | null): string {
  if (!count) return "No items";
  const items = `${count} ${count === 1 ? "item" : "items"}`;
  const skipped = results === null ? 0 : count - results;
  return skipped > 0 ? `${items}, ${skipped} skipped` : items;
}

/**
 * The run status at the end of a loop's header while a run shows: "7 of 20",
 * "20 of 20" once done, "failed at item 4"; "7 finished" when the run
 * doesn't say how many items there are; "20 of 20 · 1 error handled" once
 * items went on after a failure their step's On error handled.
 */
export function loopRunText(run: {
  finished: number;
  count: number | null;
  status: "running" | "done" | "failed" | "paused";
  failedAt: number | null;
  overLimit?: true;
  handled?: number;
}): string {
  if (run.status === "failed")
    return run.failedAt !== null
      ? `failed at item ${run.failedAt.toLocaleString("en-US")}`
      : // Before its first item: the reason when the run says it.
        run.overLimit
        ? "failed: over Up to"
        : "failed";
  const finished = run.finished.toLocaleString("en-US");
  const handled = run.handled
    ? ` · ${run.handled.toLocaleString("en-US")} ${run.handled === 1 ? "error" : "errors"} handled`
    : "";
  return run.count === null
    ? `${finished} finished${handled}`
    : `${finished} of ${run.count.toLocaleString("en-US")}${handled}`;
}

/**
 * What a list longer than Up to means, said the same way in the picker, the
 * run log and step details: how many items (when known), Up to, and what to
 * do about it.
 */
export function loopLimitText(count: number | null, limit: number): string {
  const allows = `more than Up to allows (${limit.toLocaleString("en-US")})`;
  return `${
    count === null
      ? `This list has more items than Up to allows (${limit.toLocaleString("en-US")})`
      : `This list has ${count.toLocaleString("en-US")} items, ${allows}`
  }. Raise Up to or shorten the list.`;
}

/** What a loop without a list needs, as the canvas's setup badge says it. */
const CHOOSE_LIST = "choose the list to loop over";

/** A loop's facts for its header's badge: without Items, it needs a list. */
export function loopFacts(step: Step, facts: StepFacts): StepFacts {
  if (step["items"] !== undefined || facts.setup.includes(CHOOSE_LIST))
    return facts;
  return { ...facts, setup: [...facts.setup, CHOOSE_LIST] };
}

/**
 * The badge a loop's header shows: what the loop itself needs (setup, an
 * error or a warning in its settings). How a run went shows on the steps
 * inside and in the header's run status, never twice on the frame.
 */
export function frameBadge(badge: TileBadge | null): TileBadge | null {
  return badge && ["setup", "error", "warning"].includes(badge.kind)
    ? badge
    : null;
}
