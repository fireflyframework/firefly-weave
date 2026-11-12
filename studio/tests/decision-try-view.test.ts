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
// What the Output pane and the grid's marks say about one try: each state of
// the approved design with its words.
import { describe, expect, it } from "vitest";
import {
  gridOf,
  type HitPolicy,
  type TableSpec,
} from "../src/app/editor/decision-grid/rules-model";
import {
  ruleSummary,
  tryView,
  type TryViewInput,
} from "../src/app/editor/decision-grid/try-view";
import {
  UNREACHABLE,
  type TryState,
} from "../src/app/editor/decision-grid/try-input-service";

const ref = (name: string) => ({ ref: `/input/${name}` });
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const lit = (value: unknown) => ({ literal: value });

/** payment-policy: three rules (the third a formula), and Otherwise unless asked. */
function spec(policy: HitPolicy = "first", otherwise = true): TableSpec {
  return {
    inputSchema: {
      type: "object",
      properties: { amount: { type: "number" }, tier: { type: "string" } },
    },
    outputSchema: {
      type: "object",
      properties: { approve: { type: "boolean" }, limit: { type: "number" } },
    },
    hitPolicy: policy,
    rules: [
      {
        id: "big-gold",
        when: op(
          "and",
          op("gt", ref("amount"), lit(1000)),
          op("eq", ref("tier"), lit("gold")),
        ),
        output: lit({ approve: true, limit: 5000 }),
      },
      {
        id: "small",
        when: op("lte", ref("amount"), lit(1000)),
        output: lit({ approve: true, limit: 1000 }),
      },
      {
        id: "large-or-platinum",
        when: op(
          "or",
          op("gt", ref("amount"), lit(5000)),
          op("eq", ref("tier"), lit("platinum")),
        ),
        output: lit({ approve: false, limit: 0 }),
      },
    ],
    ...(otherwise ? { defaultOutput: lit({ approve: false, limit: 0 }) } : {}),
  } as TableSpec;
}
const withFourth = (table: TableSpec): TableSpec => ({
  ...table,
  rules: [
    ...table.rules,
    {
      id: "silver-mid",
      when: op("eq", ref("tier"), lit("silver")),
      output: lit({ approve: true, limit: 2500 }),
    },
  ],
});

function input(
  table: TableSpec,
  state: Partial<TryState>,
  rest: Partial<TryViewInput> = {},
): TryViewInput {
  return {
    view: gridOf(table),
    state: {
      marks: [],
      live: null,
      checked: "checking",
      revision: 4,
      ...state,
    },
    revision: 4,
    empty: false,
    unfit: false,
    problem: null,
    ...rest,
  };
}

describe("the Output pane's view of a try", () => {
  it("has nothing to show until a value is typed", () => {
    const shown = tryView(
      input(
        spec(),
        { marks: ["no-match", "no-match", "no-match"] },
        {
          empty: true,
        },
      ),
    );
    expect(shown.kind).toBe("empty");
    expect(shown.count).toBe("Not checked");
    expect(shown.marks).toEqual([null, null, null]);
    expect(shown.pill).toBeNull();
    expect(shown.result).toBeNull();
  });

  it("shows the first match's rule card, its count, and the rules after it as not checked", () => {
    const shown = tryView(
      input(spec(), {
        marks: ["match", "no-match", "match"],
        live: {
          ok: true,
          output: { approve: true, limit: 5000 },
          matched: ["big-gold"],
          usedDefault: false,
        },
      }),
    );
    expect(shown.pill).toEqual({
      text: "Checking with Weave…",
      tone: "neutral",
    });
    expect(shown.count).toBe("Rule 1 matched");
    expect(shown.cards).toEqual([
      {
        row: 0,
        title: "Rule 1 · big-gold",
        detail: 'amount > 1000 · tier "gold"',
        tone: "success",
      },
    ]);
    expect(shown.caption).toBe("First match: rules 2 and 3 weren't checked.");
    expect(shown.marks).toEqual(["match", null, null]);
    expect(shown.result).toEqual({ value: { approve: true, limit: 5000 } });
    expect(shown.callout).toBeNull();
    expect(shown.otherwise).toBeNull();
  });

  it("says which rules didn't match before the one that won, and which weren't checked after it", () => {
    const shown = tryView(
      input(withFourth(spec()), {
        marks: ["no-match", "no-match", "match", "match"],
        live: {
          ok: true,
          output: { approve: false, limit: 0 },
          matched: ["large-or-platinum"],
          usedDefault: false,
        },
        checked: {
          answer: {
            output: { approve: false, limit: 0 },
            matched_rule_ids: ["large-or-platinum"],
            used_default: false,
          },
        },
      }),
    );
    expect(shown.pill).toEqual({ text: "Checked by Weave", tone: "success" });
    expect(shown.count).toBe("Rule 3 matched");
    expect(shown.cards[0].detail).toBe(
      'input.amount > 5000 or input.tier == "platinum"',
    );
    expect(shown.caption).toBe(
      "First match: rules 1 and 2 didn't match, so rule 3 won. Rule 4 wasn't checked.",
    );
    expect(shown.marks).toEqual(["no-match", "no-match", "match", null]);
  });

  it("shows Weave's answer once Weave has checked, even where Studio's differs", () => {
    const shown = tryView(
      input(spec(), {
        marks: ["no-match", "match", "no-match"],
        live: {
          ok: true,
          output: { approve: true, limit: 1000 },
          matched: ["small"],
          usedDefault: false,
        },
        checked: {
          answer: {
            output: { approve: true, limit: 1 },
            matched_rule_ids: ["small"],
            used_default: false,
          },
        },
      }),
    );
    expect(shown.result).toEqual({ value: { approve: true, limit: 1 } });
    expect(shown.caption).toBe(
      "First match: rule 1 didn't match, so rule 2 won. Rule 3 wasn't checked.",
    );
  });

  it("shows the Otherwise card when no rule matched", () => {
    const shown = tryView(
      input(spec(), {
        marks: ["no-match", "no-match", "no-match"],
        live: {
          ok: true,
          output: { approve: false, limit: 0 },
          matched: [],
          usedDefault: true,
        },
      }),
    );
    expect(shown.count).toBe("No rule matched");
    expect(shown.cards).toEqual([
      {
        row: null,
        title: "Otherwise",
        detail: "When no rule matches",
        tone: "neutral",
      },
    ]);
    expect(shown.result).toEqual({ value: { approve: false, limit: 0 } });
    expect(shown.marks).toEqual(["no-match", "no-match", "no-match"]);
    // The grid's Otherwise row shows it matched, as Output does.
    expect(shown.otherwise).toBe("match");
  });

  it("explains an Exactly one refusal: both rules, what a run does, and no result", () => {
    const shown = tryView(
      input(spec("unique"), {
        marks: ["match", "no-match", "match"],
        live: {
          ok: false,
          code: "WV-DECISION-MULTIPLE_MATCHES",
          path: "/spec/rules/2/when",
        },
        checked: {
          refused: {
            code: "WV-DECISION-MULTIPLE_MATCHES",
            message:
              "Two rules matched but the policy allows exactly one (rules 1 and 3).",
            path: "/spec/rules/2/when",
            rules: [1, 3],
          },
        },
      }),
    );
    expect(shown.kind).toBe("refused");
    expect(shown.pill).toEqual({ text: "Checked by Weave", tone: "neutral" });
    expect(shown.count).toBe("2 rules matched");
    expect(shown.callout).toEqual({
      tone: "danger",
      lead: "Two rules matched but the policy allows exactly one (rules 1 and 3).",
      rest: "A run with this input stops for review.",
    });
    expect(shown.cards.map((card) => [card.title, card.tone])).toEqual([
      ["Rule 1 · big-gold", "danger"],
      ["Rule 3 · large-or-platinum", "danger"],
    ]);
    expect(shown.result).toEqual({
      none: "No result for this input. Change one of the two rules so only one matches, or choose First match.",
    });
    expect(shown.marks).toEqual(["refused", "no-match", "refused"]);
  });

  it("explains the same refusal from Studio's own answer while Weave checks", () => {
    const shown = tryView(
      input(spec("unique"), {
        marks: ["match", "no-match", "match"],
        live: {
          ok: false,
          code: "WV-DECISION-MULTIPLE_MATCHES",
          path: "/spec/rules/2/when",
        },
      }),
    );
    expect(shown.pill?.text).toBe("Checking with Weave…");
    expect(shown.callout?.lead).toBe(
      "Two rules matched but the policy allows exactly one (rules 1 and 3).",
    );
    expect(shown.marks).toEqual(["refused", "no-match", "refused"]);
  });

  it("says what to do when no rule matched and there's no Otherwise result", () => {
    const shown = tryView(
      input(spec("first", false), {
        marks: ["no-match", "no-match", "no-match"],
        live: { ok: false, code: "WV-DECISION-NO_MATCH", path: "/spec" },
      }),
    );
    expect(shown.count).toBe("No rule matched");
    expect(shown.callout).toEqual({
      tone: "danger",
      lead: "No rule matched and there's no Otherwise result.",
      rest: "Add a rule for this input, or set the Otherwise result.",
    });
    expect(shown.cards).toEqual([]);
    expect(shown.result).toBeNull();
  });

  it("names an input that doesn't fit the table's input fields", () => {
    const shown = tryView(
      input(spec(), {
        marks: ["no-match", "no-match", "no-match"],
        live: {
          ok: true,
          output: { approve: false, limit: 0 },
          matched: [],
          usedDefault: true,
        },
        checked: {
          refused: {
            code: "WV-DECISION-INPUT",
            message: "The input doesn't fit the table's input fields.",
            path: "/input/amount",
            rules: [],
          },
        },
      }),
    );
    expect(shown.callout?.lead).toBe(
      "The input doesn't fit the table's input fields.",
    );
    expect(shown.result).toBeNull();
    // The rules weren't tried on it: no marks.
    expect(shown.marks).toEqual([null, null, null]);
  });

  it("says the input doesn't fit as soon as a required field is empty, without trying the rules", () => {
    const shown = tryView(
      input(
        spec(),
        { marks: ["error", "no-match", "no-match"], checked: null },
        { unfit: true },
      ),
    );
    expect(shown.callout?.lead).toBe(
      "The input doesn't fit the table's input fields.",
    );
    expect(shown.count).toBe("Not checked");
    expect(shown.marks).toEqual([null, null, null]);
    expect(shown.result).toBeNull();
    expect(shown.pill).toBeNull();
  });

  it("withholds the result while the table has a problem", () => {
    const live = {
      marks: ["match", "no-match", "no-match"] as const,
      live: {
        ok: true as const,
        output: { approve: true, limit: 5000 },
        matched: ["big-gold"],
        usedDefault: false,
      },
    };
    const shown = tryView(
      input(spec(), { ...live, marks: [...live.marks] }, { problem: 0 }),
    );
    expect(shown.kind).toBe("problem");
    expect(shown.count).toBe("Not checked");
    expect(shown.pill).toBeNull();
    expect(shown.callout).toEqual({
      tone: "warning",
      lead: "Rule 1 has a problem, so Studio can't check this input.",
      rest: "Fix the problems under the table to see the result.",
    });
    expect(shown.result).toBeNull();
    expect(shown.cards).toEqual([]);
    expect(shown.marks).toEqual([null, null, null]);
    expect(
      tryView(
        input(
          spec(),
          { ...live, marks: [...live.marks] },
          {
            problem: "table",
          },
        ),
      ).callout?.lead,
    ).toBe("The table has a problem, so Studio can't check this input.");
  });

  it("keeps the live answer when the Studio host can't be reached, and says so", () => {
    const shown = tryView(
      input(spec(), {
        marks: ["no-match", "match", "no-match"],
        live: {
          ok: true,
          output: { approve: true, limit: 1000 },
          matched: ["small"],
          usedDefault: false,
        },
        checked: "offline-host",
      }),
    );
    expect(shown.pill).toBeNull();
    expect(shown.note).toBe(UNREACHABLE);
    expect(shown.result).toEqual({ value: { approve: true, limit: 1000 } });
  });

  it("shows a card per matched rule with Collect all", () => {
    const shown = tryView(
      input(spec("collect", false), {
        marks: ["match", "no-match", "match"],
        live: {
          ok: true,
          output: [
            { approve: true, limit: 5000 },
            { approve: false, limit: 0 },
          ],
          matched: ["big-gold", "large-or-platinum"],
          usedDefault: false,
        },
      }),
    );
    expect(shown.count).toBe("2 rules matched");
    expect(shown.cards.map((card) => card.row)).toEqual([0, 2]);
    expect(shown.caption).toBe("Collect all: rule 2 didn't match.");
    expect(shown.marks).toEqual(["match", "no-match", "match"]);
  });

  it("shows nothing from an answer to an earlier version of the table", () => {
    const shown = tryView(
      input(spec(), {
        marks: ["match", "no-match", "no-match"],
        revision: 3,
        live: {
          ok: true,
          output: { approve: true, limit: 5000 },
          matched: ["big-gold"],
          usedDefault: false,
        },
      }),
    );
    expect(shown.marks).toEqual([null, null, null]);
    expect(shown.result).toBeNull();
    expect(shown.cards).toEqual([]);
  });
});

describe("a rule's conditions on one line", () => {
  it("joins each tested column with its test and leaves out the columns it doesn't test", () => {
    const view = gridOf(spec());
    expect(ruleSummary(view, 0)).toBe('amount > 1000 · tier "gold"');
    expect(ruleSummary(view, 1)).toBe("amount <= 1000");
    expect(ruleSummary(view, 2)).toBe(
      'input.amount > 5000 or input.tier == "platinum"',
    );
  });
});
