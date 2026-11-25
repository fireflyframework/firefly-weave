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
// What the activation dialog shows of a platform event trigger's row: what
// Studio does, then what it means, with who reacts and under whose authority
// in the row itself; and after the activation, what happened and what is
// still to be chosen.
import { describe, expect, it } from "vitest";
import {
  applyEventRow,
  eventBindingPlan,
  type EventRow,
  type PendingStop,
} from "../src/app/editor/activation/event-bindings";
import {
  eventResultView,
  eventRowView,
  heldResult,
  keptResult,
  resultIcon,
  type EventRowView,
} from "../src/app/editor/activation/event-rows";
import type { PlanActivation } from "../src/app/editor/activation/trigger-plan";
import type { EventDeclaration } from "../src/app/editor/triggers/event-trigger";
import { FakePlatform, refusal, requested, type Seed } from "./event-platform";

const ACTIVATIONS: PlanActivation[] = [
  { id: "a-older", workflow: "notify-team", version: "1.0.0" },
  { id: "a-old", workflow: "notify-team", version: "1.1.0" },
];
const ATTENTION: EventDeclaration = {
  events: ["transaction.in_doubt"],
  from: ["move-between-systems"],
};
const WIDER: EventDeclaration = {
  events: ["transaction.compensated", "transaction.in_doubt"],
  from: null,
};
const STORED: Seed = { id: "t-1", name: "notify-team", activation: "a-old" };

function planned(
  platform: FakePlatform,
  options: {
    declaration?: EventDeclaration | null;
    storedId?: string;
    pendingStops?: PendingStop[];
  } = {},
): EventRow[] {
  return eventBindingPlan({
    declaration:
      options.declaration === undefined ? ATTENTION : options.declaration,
    workflow: "notify-team",
    version: "1.2.0",
    storedId: options.storedId,
    pendingStops: options.pendingStops ?? [],
    bindings: platform.views(),
    activations: ACTIVATIONS,
  });
}
const seeded = (...seeds: Seed[]) => {
  const platform = new FakePlatform();
  for (const seed of seeds) platform.seed(seed);
  return platform;
};
const row = (rows: EventRow[], kind: EventRow["kind"]) => {
  const found = rows.find((item) => item.kind === kind);
  if (!found) throw Error(`No ${kind} row`);
  return found;
};
const PLAIN: EventRowView = {
  lead: "",
  detail: null,
  tone: "plain",
  icon: null,
  resolves: null,
  compare: null,
  stop: null,
  action: null,
};

describe("a row before activating", () => {
  it("creates: what it reacts to", () => {
    expect(eventRowView(row(planned(seeded()), "create"))).toEqual({
      ...PLAIN,
      lead: "Create platform event trigger notify-team",
      detail: "For Needs attention · from move-between-systems.",
    });
    expect(
      eventRowView(row(planned(seeded(), { declaration: WIDER }), "create")),
    ).toMatchObject({
      detail:
        "For Needs attention and Undone · from any workflow in this project.",
    });
  });

  it("moves: as whom runs start, and that they may resolve transactions", () => {
    const rows = planned(seeded(STORED), { storedId: "t-1" });
    expect(eventRowView(row(rows, "retarget"))).toEqual({
      ...PLAIN,
      lead: "Move platform event trigger notify-team from 1.1.0 to 1.2.0",
      detail:
        "Runs start as you after the move, including events waiting to start.",
    });
    const resolving = planned(seeded({ ...STORED, mayReconcile: true }), {
      storedId: "t-1",
    });
    expect(eventRowView(row(resolving, "retarget")).resolves).toBe(
      "Its runs may resolve transactions, as you after the move. This needs permission to reconcile transactions.",
    );
  });

  it("replaces: compares what reacts now with the new version, wider sources in warning ink", () => {
    const rows = planned(seeded(STORED), {
      storedId: "t-1",
      declaration: WIDER,
    });
    expect(eventRowView(row(rows, "replace"))).toEqual({
      ...PLAIN,
      lead: "Replace platform event trigger notify-team",
      detail:
        "1.2.0 reacts to other events and workflows, so Studio creates notify-team-2, then stops notify-team.",
      compare: {
        now: "Needs attention · from move-between-systems",
        version: "1.2.0",
        next: "Needs attention and Undone · from any workflow in this project",
        wider: true,
      },
    });
  });

  it.each<[string, EventDeclaration, string, boolean]>([
    [
      "events",
      { events: ["transaction.completed"], from: ["move-between-systems"] },
      "other events",
      false,
    ],
    [
      "workflows",
      { events: ["transaction.in_doubt"], from: ["sync-records"] },
      "other workflows",
      true,
    ],
  ])("says which of them changed: %s", (_name, declaration, words, wider) => {
    const rows = planned(seeded(STORED), { storedId: "t-1", declaration });
    const view = eventRowView(row(rows, "replace"));
    expect(view.detail).toBe(
      `1.2.0 reacts to ${words}, so Studio creates notify-team-2, then stops notify-team.`,
    );
    expect(view.compare?.wider).toBe(wider);
  });

  it("calls sources wider only when a workflow is added", () => {
    const platform = seeded({
      ...STORED,
      sources: ["move-between-systems", "sync-records"],
    });
    const fewer = planned(platform, { storedId: "t-1" });
    expect(eventRowView(row(fewer, "replace")).compare).toMatchObject({
      now: "Needs attention · from move-between-systems and sync-records",
      next: "Needs attention · from move-between-systems",
      wider: false,
    });
    const any = seeded({ ...STORED, sources: undefined });
    const narrowed = planned(any, { storedId: "t-1" });
    expect(eventRowView(row(narrowed, "replace")).compare).toMatchObject({
      now: "Needs attention · from any workflow in this project",
      wider: false,
    });
  });

  it("names every workflow in the comparison, however many", () => {
    const from = Array.from({ length: 6 }, (_, at) => `source-${at + 1}`);
    const rows = planned(seeded(STORED), {
      storedId: "t-1",
      declaration: { events: ["transaction.in_doubt"], from },
    });
    expect(eventRowView(row(rows, "replace")).compare?.next).toBe(
      "Needs attention · from source-1, source-2, source-3, source-4, source-5 and source-6",
    );
  });

  it("warns of a duplicate, with Stop it on", () => {
    const platform = seeded(STORED, {
      id: "t-2",
      name: "notify-team-2",
      activation: "a-older",
    });
    expect(
      eventRowView(row(planned(platform, { storedId: "t-2" }), "duplicate")),
    ).toEqual({
      ...PLAIN,
      lead: "Two triggers start notify-team for the same events.",
      detail:
        "Stop trigger notify-team, which an earlier activation left running. Both react until one stops.",
      tone: "warning",
      icon: "warning",
      stop: true,
    });
  });

  const owed = (triggerId: string): PendingStop => ({
    triggerId,
    revision: 1,
    activationId: "a-older",
  });
  it.each<[string, Partial<Seed>, string]>([
    [
      "other events",
      { name: "notify-team-2", events: ["transaction.completed"] },
      "notify-team-2 also starts notify-team, for other events.",
    ],
    [
      "events of other workflows",
      { name: "notify-team-2", sources: ["sync-references"] },
      "notify-team-2 also starts notify-team, for other events.",
    ],
    [
      "other events under the workflow's name",
      { name: "notify-team", events: ["transaction.completed"] },
      "Trigger notify-team also starts notify-team, for other events.",
    ],
  ])(
    "says a trigger whose stop is owed starts it for %s",
    (_what, other, lead) => {
      const platform = seeded(
        { ...STORED, name: "notify-team-3" },
        { ...STORED, id: "t-2", activation: "a-older", ...other },
      );
      const duplicate = row(
        planned(platform, { storedId: "t-1", pendingStops: [owed("t-2")] }),
        "duplicate",
      );
      expect(duplicate.text).toBe(`${lead} Stop ${other.name}`);
      expect(eventRowView(duplicate)).toMatchObject({
        lead,
        detail: `Stop trigger ${other.name}, which an earlier activation left running.`,
        stop: true,
      });
    },
  );

  it("calls a trigger that shares an event and a workflow the same, whatever else it reacts to", () => {
    const platform = seeded(
      STORED,
      {
        ...STORED,
        id: "t-2",
        name: "notify-team-2",
        activation: "a-older",
        events: ["transaction.completed", "transaction.in_doubt"],
        sources: ["sync-references", "move-between-systems"],
      },
      {
        ...STORED,
        id: "t-3",
        name: "notify-team-3",
        activation: "a-older",
        sources: undefined,
      },
    );
    const rows = planned(platform, {
      storedId: "t-1",
      pendingStops: [owed("t-2"), owed("t-3")],
    });
    expect(
      rows
        .filter((item) => item.kind === "duplicate")
        .map((item) => eventRowView(item).lead),
    ).toEqual([
      "Two triggers start notify-team for the same events.",
      "Two triggers start notify-team for the same events.",
    ]);
  });

  it("leaves a trigger made outside Studio alone, with Stop it off", () => {
    const rows = planned(
      seeded({ ...STORED, events: ["transaction.completed"] }),
    );
    expect(eventRowView(row(rows, "blocked"))).toEqual({
      ...PLAIN,
      lead: "notify-team was created outside Studio with other events.",
      detail: "Studio leaves it as it is. Change it with the command line.",
      tone: "muted",
      icon: "lock",
      stop: false,
    });
  });

  it("leaves a trigger whose authority it can't tell, with no control", () => {
    const platform = seeded({ ...STORED, mayReconcile: null });
    expect(
      eventRowView(row(planned(platform, { storedId: "t-1" }), "blocked")),
    ).toEqual({
      ...PLAIN,
      lead: "Studio can't tell whether notify-team may resolve transactions, so it leaves it as it is.",
      detail:
        "Ask an administrator to check the trigger with the command line.",
      tone: "muted",
      icon: "lock",
    });
  });

  it("says what a trigger left behind reacts to, and how it was made when Studio can tell", () => {
    const old: Seed = {
      id: "t-old",
      name: "notify-old",
      activation: "a-older",
      events: ["transaction.completed"],
      sources: ["sync-records"],
    };
    const rows = planned(seeded(old));
    expect(eventRowView(row(rows, "left-behind"))).toEqual({
      ...PLAIN,
      lead: "Platform event trigger notify-old keeps running on 1.0.0",
      detail: "Made with the command line for Completed · from sync-records.",
      stop: false,
    });
    // A name Studio gives says nothing of how it was made.
    const named = planned(seeded({ ...old, name: "notify-team-3" }));
    expect(eventRowView(row(named, "left-behind")).detail).toBe(
      "For Completed · from sync-records.",
    );
  });

  it("shows Stop it as the person left it", () => {
    const rows = planned(seeded(STORED), { declaration: null });
    const left = row(rows, "left-behind");
    expect(eventRowView({ ...left, stop: true } as EventRow).stop).toBe(true);
  });
});

/** The row applied after the activation, and what the dialog then shows of it. */
async function shown(
  platform: FakePlatform,
  applied: EventRow,
  stopWithWaiting = false,
) {
  const result = await applyEventRow(platform, applied, "a-new", {
    environment: "development",
    stopWithWaiting,
  });
  return eventResultView(applied, result);
}
const BOTH =
  "Both start notify-team for every new event until you stop it in Starts or at the next activation.";

describe("a row after activating", () => {
  it("says what was created, moved and stopped", async () => {
    expect(await shown(seeded(), row(planned(seeded()), "create"))).toEqual({
      ...PLAIN,
      lead: "Created notify-team",
      tone: "success",
      icon: "ok",
    });
    const moved = seeded(STORED);
    expect(
      await shown(moved, row(planned(moved, { storedId: "t-1" }), "retarget")),
    ).toMatchObject({ lead: "Moved notify-team to 1.2.0", tone: "success" });
    const left = seeded(STORED);
    const ticked = {
      ...row(planned(left, { declaration: null }), "left-behind"),
      stop: true,
    } as EventRow;
    expect(await shown(left, ticked)).toMatchObject({
      lead: "Stopped notify-team",
      detail: null,
      tone: "success",
    });
  });

  it("says what the replaced trigger reacts to", async () => {
    const platform = seeded(STORED);
    const replace = row(
      planned(platform, { storedId: "t-1", declaration: WIDER }),
      "replace",
    );
    expect(await shown(platform, replace)).toEqual({
      ...PLAIN,
      lead: "Replaced by notify-team-2",
      detail:
        "notify-team-2 reacts to Needs attention and Undone from any workflow in this project. notify-team is stopped.",
      tone: "success",
      icon: "ok",
    });
  });

  it("says what didn't change when a create fails, with Retry", async () => {
    const platform = seeded(STORED);
    platform.refuse.set(
      "POST /event-triggers",
      refusal(409, "WV-TRIGGER-LIMIT"),
    );
    const replace = row(
      planned(platform, { storedId: "t-1", declaration: WIDER }),
      "replace",
    );
    expect(await shown(platform, replace)).toEqual({
      ...PLAIN,
      lead: "Couldn't create notify-team-2: this environment already has 64 platform event triggers. Stop one first.",
      detail: "Nothing changed: notify-team still reacts for 1.1.0.",
      tone: "danger",
      icon: "failCircle",
      action: "retry",
    });
    // A first create changed nothing that reacts already.
    const first = seeded();
    first.refuse.set("POST /event-triggers", refusal(409, "WV-TRIGGER-LIMIT"));
    expect(await shown(first, row(planned(first), "create"))).toMatchObject({
      detail: null,
      action: "retry",
    });
  });

  it("never says nothing changed when the answer was lost", async () => {
    const platform = seeded(STORED);
    platform.lose.add("POST /event-triggers");
    const replace = row(
      planned(platform, { storedId: "t-1", declaration: WIDER }),
      "replace",
    );
    expect(await shown(platform, replace)).toMatchObject({
      detail:
        "notify-team still reacts for 1.1.0. Retry first checks whether notify-team-2 was created.",
      action: "retry",
    });
    // A platform that fails may have created it too.
    const failing = seeded(STORED);
    failing.refuse.set(
      "POST /event-triggers",
      refusal(503, "WV-UNAVAILABLE", "Try again later."),
    );
    expect((await shown(failing, replace)).detail).toContain(
      "Retry first checks whether notify-team-2 was created.",
    );
    const first = seeded();
    first.lose.add("POST /event-triggers");
    expect(await shown(first, row(planned(first), "create"))).toMatchObject({
      detail: "Retry first checks whether it was created.",
      action: "retry",
    });
    const moving = seeded(STORED);
    moving.lose.add("POST /event-triggers/t-1/retarget");
    expect(
      await shown(
        moving,
        row(planned(moving, { storedId: "t-1" }), "retarget"),
      ),
    ).toMatchObject({
      detail: "Retry first checks whether it was moved.",
      action: "retry",
    });
  });

  it("asks for the permissions a refusal needs, without Retry", async () => {
    const platform = seeded();
    platform.capabilities.delete("run.start");
    expect(await shown(platform, row(planned(platform), "create"))).toEqual({
      ...PLAIN,
      lead: "Couldn't create notify-team: you need to manage triggers, start runs and read runs in development.",
      detail:
        "Ask an administrator for these permissions, then activate again.",
      tone: "danger",
      icon: "failCircle",
    });
  });

  it("offers Stop it again when the old trigger couldn't be stopped", async () => {
    const platform = seeded(STORED);
    platform.refuse.set(
      "POST /event-triggers/t-1/disable",
      refusal(503, "WV-UNAVAILABLE", "the platform didn't answer in time"),
    );
    const replace = row(
      planned(platform, { storedId: "t-1", declaration: WIDER }),
      "replace",
    );
    expect(await shown(platform, replace)).toEqual({
      ...PLAIN,
      lead: "Created notify-team-2, but couldn't stop notify-team: the platform didn't answer in time.",
      detail: BOTH,
      tone: "warning",
      icon: "warning",
      action: "stop",
    });
  });

  it("offers no Stop it to someone who may not stop it, and says who can", async () => {
    const platform = seeded(STORED, {
      id: "t-2",
      name: "notify-team-2",
      activation: "a-older",
    });
    platform.refuse.set(
      "POST /event-triggers/t-2/disable",
      refusal(403, "WV-FORBIDDEN", "Access denied"),
    );
    const duplicate = row(planned(platform, { storedId: "t-1" }), "duplicate");
    expect(await shown(platform, duplicate)).toEqual({
      ...PLAIN,
      lead: "Couldn't stop notify-team-2: you need to manage triggers in development.",
      detail: "Ask an administrator to stop it, or for the permission.",
      tone: "warning",
      icon: "warning",
    });
  });

  it("says why a duplicate wasn't stopped while the declared trigger isn't set up", () => {
    const platform = seeded(STORED, {
      id: "t-2",
      name: "notify-team-2",
      activation: "a-older",
    });
    const duplicate = row(planned(platform, { storedId: "t-1" }), "duplicate");
    if (duplicate.kind !== "duplicate") throw Error("No duplicate");
    expect(eventResultView(duplicate, heldResult(duplicate))).toMatchObject({
      lead: "Kept notify-team-2 for now.",
      detail: "Studio stops it only after the trigger above is set up.",
      action: null,
    });
  });

  it("asks before a stop that skips waiting events", async () => {
    const platform = seeded(STORED);
    platform.starts.set("t-1", [
      requested("s-1"),
      requested("s-2"),
      requested("s-3"),
    ]);
    const replace = row(
      planned(platform, { storedId: "t-1", declaration: WIDER }),
      "replace",
    );
    expect(await shown(platform, replace)).toEqual({
      ...PLAIN,
      lead: "Created notify-team-2. notify-team has 3 events waiting to start. Stopping it now skips them.",
      detail:
        "Keep it for now and both start notify-team for every new event until you stop it in Starts or at the next activation.",
      tone: "warning",
      icon: "warning",
      action: "choose",
    });
  });

  it("asks the same when it can't check, and says what a stop skips", async () => {
    const platform = seeded(STORED);
    platform.refuse.set(
      "GET /event-triggers/t-1/starts",
      refusal(403, "WV-FORBIDDEN", "Access denied"),
    );
    const replace = row(
      planned(platform, { storedId: "t-1", declaration: WIDER }),
      "replace",
    );
    expect(await shown(platform, replace)).toMatchObject({
      lead: "Created notify-team-2. Studio can't check whether notify-team has events waiting to start.",
      detail: "Stopping it now skips any that are waiting.",
      action: "choose",
    });
  });

  it("says who keeps reacting after Keep it for now", async () => {
    const platform = seeded(STORED);
    platform.starts.set("t-1", [requested("s-1")]);
    const replace = row(
      planned(platform, { storedId: "t-1", declaration: WIDER }),
      "replace",
    );
    const asked = await applyEventRow(platform, replace, "a-new");
    const kept = keptResult(replace, asked);
    expect(kept).toMatchObject({
      status: "kept",
      id: asked.id,
      text: "Created notify-team-2. Kept notify-team for now.",
      pendingStop: { triggerId: "t-1", revision: 1, activationId: "a-old" },
    });
    expect(eventResultView(replace, kept)).toEqual({
      ...PLAIN,
      lead: "Created notify-team-2. Kept notify-team for now.",
      detail: BOTH,
      tone: "warning",
      icon: "warning",
    });
  });

  it("says both react, for different events, when the replaced trigger shares none with the new one", async () => {
    const platform = seeded(STORED);
    platform.starts.set("t-1", [requested("s-1")]);
    const replace = row(
      planned(platform, {
        storedId: "t-1",
        declaration: { events: ["transaction.completed"], from: null },
      }),
      "replace",
    );
    const until =
      "both start notify-team, for different events, until you stop it in Starts or at the next activation.";
    const asked = await applyEventRow(platform, replace, "a-new");
    expect(eventResultView(replace, asked).detail).toBe(
      `Keep it for now and ${until}`,
    );
    expect(eventResultView(replace, keptResult(replace, asked)).detail).toBe(
      `B${until.slice(1)}`,
    );
  });

  it("owes nothing for a trigger left behind that is kept, and says it keeps running", async () => {
    const platform = seeded(STORED);
    platform.starts.set("t-1", [requested("s-1")]);
    const left = {
      ...row(planned(platform, { declaration: null }), "left-behind"),
      stop: true,
    } as EventRow;
    const asked = await applyEventRow(platform, left, "a-new");
    expect(eventResultView(left, asked)).toMatchObject({
      lead: "notify-team has 1 event waiting to start. Stopping it now skips them.",
      detail: "Keep it for now and it keeps running on 1.1.0.",
      action: "choose",
    });
    const kept = keptResult(left, asked);
    expect(kept).toMatchObject({ text: "Kept notify-team for now." });
    expect(kept).not.toHaveProperty("pendingStop");
    expect(eventResultView(left, kept).detail).toBe(
      "It keeps running on 1.1.0.",
    );
  });

  it("creates nothing beside a trigger someone else made meanwhile", async () => {
    const platform = seeded();
    const create = row(planned(platform), "create");
    platform.seed(STORED);
    expect(await shown(platform, create)).toEqual({
      ...PLAIN,
      lead: "notify-team was created since you opened this. Review it.",
      detail:
        "Studio created nothing. Close this and activate again to see what it is now.",
      tone: "warning",
      icon: "warning",
    });
  });

  it("creates and stops nothing in a replace beside a trigger someone else made meanwhile", async () => {
    const platform = seeded(STORED);
    const replace = row(
      planned(platform, { storedId: "t-1", declaration: WIDER }),
      "replace",
    );
    platform.seed({
      ...STORED,
      id: "t-theirs",
      name: "notify-team-2",
      events: WIDER.events,
      sources: undefined,
    });
    expect(await shown(platform, replace)).toMatchObject({
      lead: "notify-team-2 was created since you opened this. Review it.",
      detail:
        "Studio created nothing. Close this and activate again to see what it is now.",
    });
  });

  it("gives every result of another kind of trigger its icon", () => {
    expect(
      (["created", "moved", "stopped", "failed", "changed"] as const).map(
        resultIcon,
      ),
    ).toEqual(["ok", "ok", "ok", "failCircle", "warning"]);
  });

  it("stops nothing that changed since the dialog opened", async () => {
    const platform = seeded(STORED, {
      id: "t-2",
      name: "notify-team-2",
      activation: "a-older",
    });
    const duplicate = row(planned(platform, { storedId: "t-1" }), "duplicate");
    platform.find("t-2")!["revision"] = 4;
    expect(await shown(platform, duplicate)).toEqual({
      ...PLAIN,
      lead: "notify-team-2 changed since you opened this. Review it.",
      detail:
        "Studio stopped nothing. Close this and activate again to see what it is now.",
      tone: "warning",
      icon: "warning",
    });
  });

  it("says a trigger someone else moved first wasn't moved", async () => {
    const platform = seeded(STORED);
    const move = row(planned(platform, { storedId: "t-1" }), "retarget");
    platform.find("t-1")!["revision"] = 4;
    expect(await shown(platform, move)).toMatchObject({
      lead: "notify-team changed since you opened this. Review it.",
      detail:
        "Studio moved nothing. Close this and activate again to see what it is now.",
      tone: "warning",
    });
  });

  it("says nothing reacts after stopping the one made outside Studio", async () => {
    const platform = seeded({ ...STORED, events: ["transaction.completed"] });
    const ticked = {
      ...row(planned(platform), "blocked"),
      stop: true,
    } as EventRow;
    expect(await shown(platform, ticked)).toMatchObject({
      lead: "Stopped notify-team",
      detail:
        "Activate again to create the platform event trigger this version declares.",
      tone: "success",
    });
  });

  it("leaves a row nothing was done to in place, without its box", () => {
    const platform = seeded(
      { ...STORED, events: ["transaction.completed"] },
      {
        id: "t-old",
        name: "notify-old",
        activation: "a-older",
        events: ["transaction.reconciled"],
      },
    );
    const rows = planned(platform);
    expect(eventResultView(row(rows, "left-behind"), null)).toEqual({
      ...PLAIN,
      lead: "notify-old keeps running on 1.0.0",
    });
    expect(eventResultView(row(rows, "blocked"), null)).toEqual({
      ...eventRowView(row(rows, "blocked")),
      stop: null,
    });
  });
});
