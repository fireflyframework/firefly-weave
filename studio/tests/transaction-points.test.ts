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
// Where a transaction can no longer be undone: the points of no return the
// canvas draws, what each step shows of how it is undone, and where a moved
// step may not go. The platform's compiler decides; these mirror its rule.
import { describe, expect, it } from "vitest";
import {
  pointOfNoReturnText,
  stepsInTransactions,
  undoBadge,
  undoText,
} from "../src/app/editor/canvas/transaction-frame";
import { planPaste, readClipboard } from "../src/app/editor/state/clipboard";
import { newTransactionStep } from "../src/app/editor/transactions/group";
import {
  dropAfterPointOfNoReturn,
  heldAt,
  moveRefusal,
  pointsOfNoReturn,
} from "../src/app/editor/transactions/points";
import { createStep, freshWorkflow, type Step } from "../src/app/model";

const undo = { kind: "action", uses: "a.undo@1.0.0", with: { literal: {} } };
const plain = (id: string): Step => ({ ...createStep("transform", id) });
const reserved = (id: string): Step =>
  ({
    id,
    kind: "action",
    uses: "a.hold@1.0.0",
    with: { literal: {} },
    confirm: undo,
    cancel: undo,
  }) as unknown as Step;
const reversible = (id: string): Step =>
  ({
    id,
    kind: "action",
    uses: "a.add@1.0.0",
    with: { literal: {} },
    compensate: undo,
  }) as unknown as Step;
const last = (id: string): Step =>
  ({
    id,
    kind: "action",
    uses: "a.submit@1.0.0",
    with: { literal: {} },
    irreversible: true,
  }) as unknown as Step;
const guarded = (step: Step, path: Step[]): Step => ({
  ...step,
  onError: "errorOutput",
  errorPath: { steps: path, output: { literal: null } },
});
const move = (...steps: Step[]): Step => ({
  ...newTransactionStep("move"),
  steps,
});
const decision = (id: string, ...paths: Step[][]): Step =>
  ({
    ...createStep("switch", id),
    cases: paths.slice(0, -1).map((steps) => ({
      when: { literal: true },
      steps,
      output: { literal: null },
    })),
    default: { steps: paths[paths.length - 1], output: { literal: null } },
  }) as Step;
const fork = (id: string, ...branches: Step[][]): Step =>
  ({
    ...createStep("parallel", id),
    branches: Object.fromEntries(
      branches.map((steps, index) => [
        `branch-${index + 1}`,
        { steps, output: { literal: null } },
      ]),
    ),
  }) as Step;
const loop = (id: string, ...steps: Step[]): Step =>
  ({
    ...createStep("forEach", id),
    items: { ref: "/input/moves" },
    body: { steps, output: { ref: "/item" } },
  }) as Step;
const points = (...steps: Step[]) => [...pointsOfNoReturn(move(...steps))];

describe("how a step shows it is undone", () => {
  it("has an icon and a word for each way", () => {
    expect(undoBadge(reserved("hold"))).toEqual({
      icon: "lock",
      label: "Reserved",
    });
    expect(undoBadge(reversible("add"))).toEqual({
      icon: "undo",
      label: "Reversible",
    });
    expect(undoBadge(last("submit"))).toEqual({
      icon: "critical",
      label: "Can't be undone",
    });
  });

  it("has none for a step that only reads, changes nothing or hasn't said", () => {
    const action = { uses: "a.lookup@1.0.0", with: { literal: {} } };
    expect(
      undoBadge({ id: "lookup", kind: "action", ...action } as unknown as Step),
    ).toBeNull();
    expect(undoBadge(plain("build"))).toBeNull();
  });

  it("reads out as the end of the step's name", () => {
    expect(undoText(reversible("add"))).toBe("can be undone by reversing it");
    expect(undoText(reserved("hold"))).toBe("is reserved, then confirmed");
    expect(undoText(last("submit"))).toBe("can't be undone");
    expect(undoText(plain("build"))).toBe("");
  });
});

describe("which steps are in a transaction", () => {
  it("is its own steps at any depth, and neither it nor its error path", () => {
    const tiles = [
      { id: "lookup", owner: "root" },
      { id: "move", owner: "root" },
      { id: "hold", owner: "move/steps" },
      { id: "route", owner: "move/steps" },
      { id: "submit", owner: "route/case 1" },
      { id: "flag", owner: "submit/errorPath" },
      { id: "report", owner: "move/errorPath" },
      { id: "moves", owner: "root" },
      { id: "check", owner: "moves/body" },
    ];
    expect([
      ...stepsInTransactions(tiles, [
        { id: "move", transaction: true },
        { id: "moves" },
      ]),
    ]).toEqual(["hold", "route", "submit", "flag"]);
    expect(stepsInTransactions(tiles, [{ id: "moves" }]).size).toBe(0);
  });
});

describe("points of no return", () => {
  it("is the first step that can't be undone, and only that one", () => {
    expect(points(reserved("hold"), reversible("add"))).toEqual([]);
    expect(
      points(reserved("hold"), last("submit"), plain("build"), last("again")),
    ).toEqual(["submit"]);
  });

  it("is one per decision path that holds one", () => {
    expect(
      points(
        reserved("hold"),
        decision(
          "route",
          [last("submit"), plain("build")],
          [reversible("add")],
        ),
      ),
    ).toEqual(["submit"]);
    expect(
      points(decision("route", [last("submit")], [last("submit-batch")])),
    ).toEqual(["submit", "submit-batch"]);
  });

  it("goes on after a decision only for the paths that can still be undone", () => {
    expect(
      points(
        decision("route", [last("submit")], [plain("build")]),
        last("finish"),
      ),
    ).toEqual(["submit", "finish"]);
    expect(
      points(
        decision("route", [last("submit")], [last("submit-batch")]),
        last("finish"),
      ),
    ).toEqual(["submit", "submit-batch"]);
    // A path that stops never reaches the steps after the decision.
    expect(
      points(
        decision("route", [last("submit")], [createStep("fail", "stop")]),
        last("finish"),
      ),
    ).toEqual(["submit"]);
    // Nothing inside a decision counts once the transaction has passed one.
    expect(
      points(last("submit"), decision("route", [last("again")], [])),
    ).toEqual(["submit"]);
  });

  it("is in a parallel branch when no other one holds a step that may change data", () => {
    expect(points(fork("both", [last("submit")], [plain("build")]))).toEqual([
      "submit",
    ]);
    // The platform refuses it there, however deep the other step is.
    for (const other of [
      reversible("add"),
      decision("route", [reversible("add")], []),
      guarded(plain("build"), [last("again")]),
      { ...reversible("add"), kind: "callWorkflow" } as unknown as Step,
    ])
      expect(
        points(fork("both", [last("submit")], [plain("note"), other])),
      ).toEqual([]);
    // Around it too.
    expect(
      points(
        fork("out", [fork("in", [last("submit")], [])], [reversible("add")]),
      ),
    ).toEqual([]);
    expect(points(loop("each", fork("in", [last("submit")], [])))).toEqual([]);
  });

  it("draws none inside a loop: the platform refuses it there", () => {
    expect(points(loop("each", last("submit")))).toEqual([]);
    // A loop may run no item, so what follows it can still decide.
    expect(points(loop("each", last("submit")), last("finish"))).toEqual([
      "finish",
    ]);
    // Every branch runs: after one that can't be undone, nothing can.
    expect(
      points(
        fork("both", [last("submit")], [reversible("add")]),
        last("finish"),
      ),
    ).toEqual([]);
  });

  it("can be in a step's error path, when the step itself isn't one", () => {
    expect(points(guarded(reversible("add"), [last("submit")]))).toEqual([
      "submit",
    ]);
    expect(points(guarded(last("submit"), [last("retry")]))).toEqual([
      "submit",
    ]);
  });

  it("goes on after a step's error path for a run in which the step didn't fail", () => {
    expect(
      points(guarded(plain("check"), [last("submit")]), last("finish")),
    ).toEqual(["submit", "finish"]);
  });

  it("never counts the transaction's own error path", () => {
    expect([
      ...pointsOfNoReturn(guarded(move(reserved("hold")), [last("report")])),
    ]).toEqual([]);
  });

  it("names the line by its step and says what it means", () => {
    expect(pointOfNoReturnText("submit")).toEqual({
      name: "Point of no return, after submit",
      description:
        "Once submit succeeds, the transaction finishes. Steps after this line retry until they succeed and are never undone.",
    });
  });
});

const REASON =
  "Steps that can be undone must come before the step that can't be undone.";
describe("where a step that can be undone may go", () => {
  const body = move(
    reserved("hold"),
    reversible("add"),
    last("submit"),
    plain("build"),
  );
  it("is before the step that can't be undone, never after it", () => {
    const drop = (index: number, moving = reversible("add")) =>
      dropAfterPointOfNoReturn(body, moving, "move/steps", index);
    expect([0, 1, 2, 3, 4].map((index) => drop(index))).toEqual([
      null,
      null,
      null,
      REASON,
      REASON,
    ]);
    expect(drop(4, reserved("other"))).toBe(REASON);
  });

  it("is anywhere for a step that changes nothing or that can't be undone", () => {
    expect(
      dropAfterPointOfNoReturn(body, plain("build"), "move/steps", 4),
    ).toBeNull();
    expect(
      dropAfterPointOfNoReturn(body, last("finish"), "move/steps", 4),
    ).toBeNull();
  });

  it("counts every step inside a moved block", () => {
    for (const block of [
      decision("route", [plain("build")], [reversible("add")]),
      loop("each", reserved("other")),
      guarded(plain("check"), [reversible("add")]),
    ])
      expect(dropAfterPointOfNoReturn(body, block, "move/steps", 4)).toBe(
        REASON,
      );
    expect(
      dropAfterPointOfNoReturn(
        body,
        loop("each", plain("other")),
        "move/steps",
        4,
      ),
    ).toBeNull();
  });

  it("is refused inside a block that runs after the point, and after a decision one of whose paths has one", () => {
    const route = decision("route", [last("submit")], [plain("build")]);
    const inside = move(reserved("hold"), route, loop("each"));
    const drop = (owner: string, index: number) =>
      dropAfterPointOfNoReturn(inside, reversible("add"), owner, index);
    expect(drop("move/steps", 1)).toBeNull();
    expect(drop("route/case 1", 0)).toBeNull();
    expect(drop("route/case 1", 1)).toBe(REASON);
    expect(drop("route/default", 1)).toBeNull();
    expect(drop("move/steps", 2)).toBe(REASON);
    expect(drop("each/body", 0)).toBe(REASON);
  });

  it("doesn't count the moved step itself", () => {
    const route = decision("route", [reversible("add"), last("submit")], []);
    expect(
      dropAfterPointOfNoReturn(
        move(route, plain("build")),
        route,
        "move/steps",
        2,
      ),
    ).toBeNull();
  });

  it("is refused in the error path of the step that can't be undone", () => {
    const body = move(guarded(last("submit"), []));
    expect(
      dropAfterPointOfNoReturn(body, reversible("add"), "submit/errorPath", 0),
    ).toBe(REASON);
  });
});

describe("where a step that can be undone may go after a parallel step", () => {
  const drop = (...branches: Step[][]) =>
    dropAfterPointOfNoReturn(
      move(fork("both", ...branches)),
      reversible("add"),
      "move/steps",
      1,
    );
  it("is nowhere once any of its branches passed a point of no return", () => {
    expect(drop([last("submit")], [plain("build")])).toBe(REASON);
    expect(drop([plain("check")], [plain("build")])).toBeNull();
  });

  it("has no reason where no run gets to: one of its branches always stops", () => {
    expect(drop([last("submit")], [createStep("fail", "stop")])).toBeNull();
  });
});

describe("what a waiting step finds held", () => {
  const pause = (): Step => createStep("wait", "pause");
  const held = (...steps: Step[]) =>
    heldAt(move(...steps), "pause").map(({ id }) => id);
  it("is what was reserved before it on a path that can still be undone", () => {
    expect(held(reserved("a"), reversible("add"), pause())).toEqual(["a"]);
    expect(held(pause(), reserved("a"))).toEqual([]);
    // That path's reservations are confirmed, and the other one made none.
    expect(
      held(
        decision("route", [reserved("a"), last("submit")], [plain("build")]),
        pause(),
      ),
    ).toEqual([]);
    expect(held(reserved("a"), last("submit"), pause())).toEqual([]);
  });

  it("is what the other branches around it can reserve, never what comes after it in its own", () => {
    expect(
      held(fork("both", [pause(), reserved("own")], [reserved("other")])),
    ).toEqual(["other"]);
    expect(
      held(
        fork(
          "both",
          [reserved("outer")],
          [fork("inner", [pause()], [plain("build")])],
        ),
      ),
    ).toEqual(["outer"]);
  });

  it("is what the loop's last item reserved, unless it runs one", () => {
    const each = (maxItems: number) =>
      held({ ...loop("each", pause(), reserved("a")), maxItems });
    expect(each(3)).toEqual(["a"]);
    expect(each(1)).toEqual([]);
  });
});

const NESTED = "A transaction can't contain another transaction.";
describe("moving or pasting a transaction into another", () => {
  const workflow = freshWorkflow();
  const other = { ...newTransactionStep("other"), steps: [plain("check")] };
  workflow.spec.steps = [
    guarded(move(reserved("hold"), last("submit")), []),
    other,
    loop("each", { ...newTransactionStep("inner") }),
    reversible("add"),
  ];
  it("is refused wherever its steps or a block's inside it would hold it", () => {
    expect(
      moveRefusal(workflow, other, { owner: "move/steps", index: 0 }),
    ).toBe(NESTED);
    expect(
      moveRefusal(workflow, workflow.spec.steps[2], {
        owner: "move/steps",
        index: 0,
      }),
    ).toBe(NESTED);
    // Its error path runs after it: not inside it.
    expect(
      moveRefusal(workflow, other, { owner: "move/errorPath", index: 0 }),
    ).toBeNull();
    expect(
      moveRefusal(workflow, other, { owner: "each/body", index: 0 }),
    ).toBeNull();
    expect(
      moveRefusal(workflow, other, { owner: "root", index: 0 }),
    ).toBeNull();
  });

  it("goes with the rule on the point of no return", () => {
    const add = workflow.spec.steps[3];
    expect(
      moveRefusal(workflow, add, { owner: "move/steps", index: 1 }),
    ).toBeNull();
    expect(moveRefusal(workflow, add, { owner: "move/steps", index: 2 })).toBe(
      REASON,
    );
    expect(
      moveRefusal(workflow, add, { owner: "other/steps", index: 1 }),
    ).toBeNull();
  });

  const paste = (steps: Step[], owner: string) => {
    const reading = readClipboard(JSON.stringify({ weaveClipboard: 1, steps }));
    if (!reading.ok) throw new Error(reading.reason);
    return planPaste(
      workflow,
      reading.content,
      { owner, index: 0 },
      new Set(["move", "hold", "submit", "other", "check", "each", "inner"]),
    );
  };
  it("is refused when pasted, with nothing changed", () => {
    expect(paste([other], "move/steps")).toEqual({ reason: NESTED });
    expect(
      paste([loop("again", newTransactionStep("inner-2"))], "move/steps"),
    ).toEqual({ reason: NESTED });
    expect(paste([plain("note")], "move/steps")).toMatchObject({
      ids: ["note"],
    });
    expect(paste([other], "move/errorPath")).toMatchObject({
      ids: ["other-1"],
    });
    expect(paste([other], "root")).toMatchObject({ ids: ["other-1"] });
  });
});
