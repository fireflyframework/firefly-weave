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
// What one try of the table says, in the Output pane and as each rule's mark
// in the grid: Weave's answer once it has checked, Studio's live answer
// until then. A problem in the table withholds the answer, because the table
// being tried isn't the one the grid shows.
import type { Json } from "../../forms/core/json";
import { printFormula } from "../formula/printer";
import { printCell } from "./cell-grammar";
import type { RowMark } from "./table-evaluate";
import type { GridView } from "./rules-model";
import {
  BUSY,
  UNREACHABLE,
  explainRefusal,
  type TryState,
} from "./try-input-service";

/** A rule's mark in the grid's # column; null: not checked, or nothing tried. */
export type GridMark = "match" | "no-match" | "refused" | "error";
export interface TryCard {
  /** The rule's row, from 0; null for Otherwise. */
  row: number | null;
  title: string;
  detail: string;
  tone: "success" | "danger" | "neutral";
}
export interface TryView {
  kind: "empty" | "pending" | "problem" | "answer" | "refused";
  pill: { text: string; tone: "success" | "neutral" } | null;
  /** "Rule 1 matched", "2 rules matched", "No rule matched", "Not checked". */
  count: string;
  callout: { tone: "danger" | "warning"; lead: string; rest: string } | null;
  cards: TryCard[];
  /** One line about the rules that didn't decide the answer. */
  caption: string | null;
  result: { value: Json } | { none: string } | null;
  /** Why only Studio's live answer shows. */
  note: string | null;
  marks: (GridMark | null)[];
  /** The Otherwise row's mark: "match" when its result answered. */
  otherwise: "match" | null;
}
export interface TryViewInput {
  view: GridView;
  state: TryState;
  /** The table's revision the grid shows: an answer to another is not shown. */
  revision: number;
  /** No test value yet. */
  empty: boolean;
  /** A required input field is empty: the input doesn't fit the table. */
  unfit: boolean;
  /** The first error in the table: its rule (from 0), "table" for one on no rule. */
  problem: number | "table" | null;
}

/** The host's words for its refusals (studio/table_check.py), for Studio's live answer. */
const REFUSALS: Readonly<Record<string, string>> = {
  "WV-DECISION-MULTIPLE_MATCHES":
    "Two rules matched but the policy allows exactly one.",
  "WV-DECISION-NO_MATCH": "No rule matched and there's no Otherwise result.",
  "WV-DECISION-INPUT": "The input doesn't fit the table's input fields.",
  "WV-DECISION-OUTPUT": "The result doesn't fit the table's result fields.",
  "WV-DECISION-PREDICATE": "A rule's condition isn't true or false.",
  "WV-DECISION-REFERENCE": "Rules can read only the table's input.",
  "WV-DECISION-CONTRACT": "This isn't a decision table Weave can run.",
};
const COULDNT = "A rule couldn't be evaluated with this input.";
const MULTIPLE = "WV-DECISION-MULTIPLE_MATCHES";
const NO_MATCH = "WV-DECISION-NO_MATCH";
const INPUT = "WV-DECISION-INPUT";

/** "rule 4", "rules 2 and 3", "rules 2 to 9". */
function rules(from: number, to: number): string {
  if (from === to) return `rule ${from}`;
  return to === from + 1 ? `rules ${from} and ${to}` : `rules ${from} to ${to}`;
}
const capital = (text: string) => text[0].toUpperCase() + text.slice(1);
const matchedCount = (count: number) =>
  count === 0
    ? "No rule matched"
    : count === 1
      ? "One rule matched"
      : `${count} rules matched`;

/** A rule's conditions on one line: `amount > 1000 · tier "gold"`, or its formula. */
export function ruleSummary(view: GridView, row: number): string {
  const condition = view.rows[row]?.condition;
  if (!condition) return "";
  if (condition.kind === "formula")
    return printFormula(condition.when) ?? JSON.stringify(condition.when);
  const parts = condition.cells.flatMap((cell, index) => {
    if (cell === null) return [];
    const column = view.inputs[index];
    const text =
      printCell(cell, { name: column.name, type: column.type ?? "text" }) ??
      printFormula(cell) ??
      JSON.stringify(cell);
    return [`${column.name} ${text}`];
  });
  return parts.length ? parts.join(" · ") : "Any input";
}

function card(view: GridView, row: number, tone: TryCard["tone"]): TryCard {
  return {
    row,
    title: `Rule ${row + 1} · ${view.rows[row]?.id ?? ""}`,
    detail: ruleSummary(view, row),
    tone,
  };
}

/** What the other rules did, by hit policy. */
function caption(view: GridView, rows: number[]): string | null {
  const total = view.rows.length;
  if (view.hitPolicy === "first" && rows.length === 1) {
    const won = rows[0] + 1;
    const before =
      won > 1 ? `${rules(1, won - 1)} didn't match, so rule ${won} won.` : "";
    const after =
      won < total
        ? `${rules(won + 1, total)} ${won + 1 === total ? "wasn't" : "weren't"} checked.`
        : "";
    if (!before && !after) return null;
    return `First match: ${before ? `${before}${after ? ` ${capital(after)}` : ""}` : after}`;
  }
  if (view.hitPolicy === "collect" && rows.length) {
    const missed = view.rows
      .map((_, index) => index)
      .filter((index) => !rows.includes(index))
      .map((index) => index + 1);
    if (!missed.length) return null;
    return `Collect all: ${missed.length === 1 ? `rule ${missed[0]} didn't` : `rules ${missed.slice(0, -1).join(", ")} and ${missed.at(-1)} didn't`} match.`;
  }
  return null;
}

export function tryView(input: TryViewInput): TryView {
  const { view, state, revision } = input;
  const none = view.rows.map(() => null);
  const blank: TryView = {
    kind: "empty",
    pill: null,
    count: "",
    callout: null,
    cards: [],
    caption: null,
    result: null,
    note: null,
    marks: none,
    otherwise: null,
  };
  if (input.empty) return { ...blank, count: "Not checked" };
  if (input.problem !== null)
    return {
      ...blank,
      kind: "problem",
      count: "Not checked",
      callout: {
        tone: "warning",
        lead:
          input.problem === "table"
            ? "The table has a problem, so Studio can't check this input."
            : `Rule ${input.problem + 1} has a problem, so Studio can't check this input.`,
        rest: "Fix the problems under the table to see the result.",
      },
    };
  // An input that doesn't fit isn't tried by Studio: no marks, no result.
  // Weave still checks it, and its refusal says what doesn't fit.
  const unfit: TryView = {
    ...blank,
    kind: "refused",
    count: "Not checked",
    callout: { tone: "danger", lead: REFUSALS[INPUT], rest: "" },
  };
  if (state.revision !== revision || !state.live)
    return input.unfit ? unfit : { ...blank, kind: "pending" };

  const { checked, live } = state;
  const pill: TryView["pill"] =
    checked === "checking"
      ? { text: "Checking with Weave…", tone: "neutral" }
      : checked && typeof checked === "object"
        ? "answer" in checked
          ? { text: "Checked by Weave", tone: "success" }
          : { text: "Checked by Weave", tone: "neutral" }
        : null;
  const note =
    checked === "offline-host"
      ? UNREACHABLE
      : checked === "busy-host"
        ? BUSY
        : null;
  const marks: (GridMark | null)[] = view.rows.map(
    (_, index): GridMark | null => (state.marks[index] as RowMark) ?? null,
  );

  // Weave's refusal once it has checked; Studio's own until then.
  const refusal =
    checked && typeof checked === "object" && "refused" in checked
      ? checked.refused
      : !(checked && typeof checked === "object" && "answer" in checked) &&
          !live.ok
        ? {
            code: live.code,
            path: live.path,
            ...explainRefusal(
              {
                code: live.code,
                message: REFUSALS[live.code] ?? COULDNT,
                path: live.path,
              },
              state.marks,
            ),
          }
        : null;
  if (input.unfit) {
    const weave =
      checked && typeof checked === "object" && "refused" in checked
        ? checked.refused
        : null;
    return {
      ...unfit,
      count: weave ? "" : unfit.count,
      pill: checked === "checking" || weave ? pill : null,
      note,
      callout: weave
        ? { ...unfit.callout!, lead: weave.message }
        : unfit.callout,
    };
  }
  if (refusal?.code === INPUT)
    return {
      ...unfit,
      pill,
      note,
      callout: { ...unfit.callout!, lead: refusal.message },
    };
  if (refusal) {
    const shown: TryView = {
      ...blank,
      kind: "refused",
      pill,
      note,
      marks,
      callout: { tone: "danger", lead: refusal.message, rest: "" },
    };
    if (refusal.code === MULTIPLE) {
      const refused = refusal.rules.map((rule) => rule - 1);
      return {
        ...shown,
        count: matchedCount(refused.length),
        callout: {
          tone: "danger",
          lead: refusal.message,
          rest: "A run with this input stops for review.",
        },
        cards: refused.map((row) => card(view, row, "danger")),
        result: {
          none: `No result for this input. ${refused.length === 2 ? "Change one of the two rules" : "Change the rules"} so only one matches, or choose First match.`,
        },
        marks: marks.map((mark, index) =>
          refused.includes(index) ? "refused" : mark,
        ),
      };
    }
    if (refusal.code === NO_MATCH)
      return {
        ...shown,
        count: "No rule matched",
        callout: {
          tone: "danger",
          lead: refusal.message,
          rest: "Add a rule for this input, or set the Otherwise result.",
        },
      };
    return shown;
  }

  const answer =
    checked && typeof checked === "object" && "answer" in checked
      ? {
          output: checked.answer.output,
          matched: checked.answer.matched_rule_ids,
          usedDefault: checked.answer.used_default,
        }
      : live.ok
        ? live
        : null;
  if (!answer) return { ...blank, kind: "pending", pill, note };
  const ids = view.rows.map((row) => row.id);
  const rows = answer.matched
    .map((id) => ids.indexOf(id))
    .filter((row) => row >= 0);
  // First match stops at the rule that won: the rules after it weren't checked.
  const checkedMarks =
    view.hitPolicy === "first" && rows.length
      ? marks.map((mark, index) => (index > rows[0] ? null : mark))
      : marks;
  return {
    ...blank,
    kind: "answer",
    pill,
    note,
    marks: checkedMarks,
    count:
      rows.length === 1
        ? `Rule ${rows[0] + 1} matched`
        : matchedCount(rows.length),
    cards: answer.usedDefault
      ? [
          {
            row: null,
            title: "Otherwise",
            detail: "When no rule matches",
            tone: "neutral",
          },
        ]
      : rows.map((row) => card(view, row, "success")),
    caption: caption(view, rows),
    result: { value: answer.output },
    otherwise: answer.usedDefault ? "match" : null,
  };
}
