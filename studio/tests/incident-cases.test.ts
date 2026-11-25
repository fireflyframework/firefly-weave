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
// How the Incidents inbox groups a caller's settle timeout with the in-doubt
// incidents of the called runs it waits for: one case per group, and every
// loaded incident in exactly one row.
import { describe, expect, it } from "vitest";
import {
  caseLabel,
  inboxRows,
  linkPlace,
  missingText,
  rowGroups,
  type InboxRow,
  type IncidentCase,
} from "../src/app/operate/incidents/incident-cases";
import type {
  IncidentLink,
  IncidentRecord,
  IncidentView,
} from "../src/app/operate/incidents/incidents-model";
import {
  settleTimeout,
  undoStuck,
  withheldLinks,
} from "./contracts/transaction-examples";

const plain = (id: string): IncidentView => ({
  id,
  run_id: "8a2d5e61-0000-4000-8000-000000000001",
  incident_key: "send:0",
  node_id: "send",
  generation: 0,
  origin_code: "WV-TASK-AMBIGUOUS",
  code: "WV-TASK-AMBIGUOUS",
  status: "active",
  revision: 1,
  external_effects_may_continue: false,
});
const linkOf = (incident: IncidentView): IncidentLink => ({
  id: incident.id,
  run_id: incident.run_id,
  code: incident.code,
  status: incident.status,
  node_id: incident.node_id,
  ...(incident.instance_key ? { instance_key: incident.instance_key } : {}),
  ...(incident.workflow ? { workflow: incident.workflow } : {}),
});
/** A row in short: "case(lead, member…; missing…)", or the incident's ID. */
const short = (row: InboxRow): string =>
  row.kind === "incident"
    ? row.value.id
    : `case(${row.value.members.map((item) => item.id).join(", ")}; ${row.value.missing
        .map((item) => item.id)
        .join(", ")})`;
const shape = (rows: { high: InboxRow[]; normal: InboxRow[] }) => ({
  high: rows.high.map(short),
  normal: rows.normal.map(short),
});
const onlyCase = (rows: InboxRow[]): IncidentCase => {
  const found = rows.flatMap((row) => (row.kind === "case" ? [row.value] : []));
  expect(found).toHaveLength(1);
  return found[0];
};
/** The incidents a band's rows hold, in order. */
const held = (rows: InboxRow[]) =>
  rows.flatMap((row) =>
    row.kind === "case"
      ? row.value.members.map((item) => item.id)
      : [row.value.id],
  );

describe("the inbox's cases", () => {
  it("a settle timeout and the loaded incident it waits for are one case, the called run's to resolve first", () => {
    const rows = inboxRows([settleTimeout, undoStuck], []);
    expect(shape(rows)).toEqual({
      high: [`case(${settleTimeout.id}, ${undoStuck.id}; )`],
      normal: [],
    });
    const found = onlyCase(rows.high);
    expect(found.key).toBe(settleTimeout.id);
    expect(found.lead).toBe(settleTimeout);
    expect(found.resolveFirst).toEqual(settleTimeout.waits_on![0]);
  });

  it("a called run's incident whose caller's isn't loaded leads its own case, to resolve first, with the caller's missing", () => {
    const rows = inboxRows([undoStuck], []);
    expect(shape(rows).high).toEqual([
      `case(${undoStuck.id}; ${settleTimeout.id})`,
    ]);
    const found = onlyCase(rows.high);
    expect(found.resolveFirst).toEqual(linkOf(undoStuck));
    expect(found.missing).toEqual([undoStuck.held_by]);
  });

  it("a settle timeout whose called run's incident isn't loaded names it as missing, and to resolve first", () => {
    const found = onlyCase(inboxRows([settleTimeout], []).high);
    expect(found.members).toEqual([settleTimeout]);
    expect(found.missing).toEqual(settleTimeout.waits_on);
    expect(found.resolveFirst).toEqual(settleTimeout.waits_on![0]);
  });

  it("a called run's incident in the other band still folds into its caller's case, and no incident shows twice", () => {
    const other = plain("d0000000-0000-4000-8000-000000000001");
    const rows = inboxRows([settleTimeout], [other, undoStuck]);
    expect(shape(rows)).toEqual({
      high: [`case(${settleTimeout.id}, ${undoStuck.id}; )`],
      normal: [other.id],
    });
  });

  it("an incident both bands answered shows once, in the first", () => {
    const rows = inboxRows(
      [settleTimeout, undoStuck],
      [undoStuck, settleTimeout],
    );
    expect(held([...rows.high, ...rows.normal])).toEqual([
      settleTimeout.id,
      undoStuck.id,
    ]);
    expect(rows.normal).toEqual([]);
  });

  it("two callers waiting for one called run: its incident folds into the first, and the second names it as missing", () => {
    const second: IncidentView = {
      ...settleTimeout,
      id: "6d18b3f0-0000-4000-8000-000000000002",
      run_id: "4f2c1a9e-0000-4000-8000-000000000002",
    };
    const rows = inboxRows([settleTimeout, second, undoStuck], []);
    expect(shape(rows).high).toEqual([
      `case(${settleTimeout.id}, ${undoStuck.id}; )`,
      `case(${second.id}; ${undoStuck.id})`,
    ]);
  });

  it("a called run that waits for its own called run joins its caller's case, and the last one is to resolve first", () => {
    // parent waits for the middle run's settle timeout, which waits for the
    // called run's undo.
    const middle: IncidentView = {
      ...settleTimeout,
      id: "6d18b3f0-0000-4000-8000-0000000000aa",
      run_id: "4f2c1a9e-0000-4000-8000-0000000000aa",
      node_id: "elsewhere",
      held_by: linkOf({ ...settleTimeout, waits_on: undefined }),
    };
    const parent: IncidentView = {
      ...settleTimeout,
      waits_on: [linkOf(middle)],
    };
    const child: IncidentView = { ...undoStuck, held_by: linkOf(middle) };
    for (const order of [
      [parent, middle, child],
      [child, middle, parent],
      [middle, child, parent],
    ]) {
      const rows = inboxRows(order, []);
      expect(shape(rows).high).toEqual([
        `case(${parent.id}, ${middle.id}, ${child.id}; )`,
      ]);
      expect(onlyCase(rows.high).resolveFirst).toEqual(linkOf(child));
    }
    // Without the parent, the middle run leads: it waits, and is waited for.
    const found = onlyCase(inboxRows([child, middle], []).high);
    expect(found.lead).toBe(middle);
    expect(found.members).toEqual([middle, child]);
    expect(found.missing).toEqual([middle.held_by]);
    expect(found.resolveFirst).toEqual(linkOf(child));
  });

  it("resolve first is the first one still active, and none when they are all decided", () => {
    const link = settleTimeout.waits_on![0];
    const decided = {
      ...link,
      id: "2b7e4c19-0000-4000-8000-000000000009",
      status: "resolved" as const,
    };
    const waiting = (waits_on: IncidentLink[]) =>
      onlyCase(inboxRows([{ ...settleTimeout, waits_on }], []).high);
    expect(waiting([decided, link]).resolveFirst).toEqual(link);
    expect(waiting([decided]).resolveFirst).toBeNull();
  });

  it("a settle timeout whose links were withheld is a plain row, and so is an incident with no links", () => {
    expect(withheldLinks.waits_on).toBeUndefined();
    const alone: IncidentView = { ...undoStuck };
    delete alone.held_by;
    expect(shape(inboxRows([withheldLinks, alone], [])).high).toEqual([
      withheldLinks.id,
      alone.id,
    ]);
  });

  it("a called run's incident is a plain row beside a caller's that doesn't list it", () => {
    const { waits_on: _links, ...unlisting } = settleTimeout;
    const caller: IncidentView = { ...unlisting, waits_on_more: true };
    expect(shape(inboxRows([caller, undoStuck], [])).high).toEqual([
      caller.id,
      undoStuck.id,
    ]);
  });

  it("an incident Studio can't show stays a plain row, and the case still names it", () => {
    const unavailable: IncidentRecord = { id: undoStuck.id, unavailable: true };
    expect(shape(inboxRows([settleTimeout, unavailable], [])).high).toEqual([
      `case(${settleTimeout.id}; ${undoStuck.id})`,
      unavailable.id,
    ]);
  });

  it("a case sits where its first member sat", () => {
    const before = plain("a0000000-0000-4000-8000-000000000001");
    const between = plain("b0000000-0000-4000-8000-000000000001");
    const after = plain("c0000000-0000-4000-8000-000000000001");
    expect(
      shape(inboxRows([before, undoStuck, between, settleTimeout, after], []))
        .high,
    ).toEqual([
      before.id,
      `case(${settleTimeout.id}, ${undoStuck.id}; )`,
      between.id,
      after.id,
    ]);
  });

  it("incidents that wait for each other still show once each", () => {
    const one: IncidentView = {
      ...settleTimeout,
      waits_on: [linkOf({ ...settleTimeout, id: undoStuck.id })],
    };
    const two: IncidentView = {
      ...undoStuck,
      held_by: linkOf(settleTimeout),
      waits_on: [linkOf(settleTimeout)],
    };
    const rows = inboxRows([one, two], []);
    expect(held(rows.high)).toEqual([one.id, two.id]);
    expect(onlyCase(rows.high).resolveFirst).not.toBeNull();
  });
});

describe("the groups the table draws", () => {
  it("keep their keys as incidents come and go, so a refresh draws no row again", () => {
    const [one, two, three] = ["a", "b", "c"].map((letter) =>
      plain(`${letter}0000000-0000-4000-8000-000000000001`),
    );
    const keys = (items: IncidentRecord[]) =>
      rowGroups(inboxRows(items, []).high).map((group) => group.key);
    const before = keys([one, settleTimeout, undoStuck, two]);
    expect(new Set(before).size).toBe(3);
    // An incident opens before each group's first; another leaves.
    expect(keys([three, one, settleTimeout, undoStuck, two])).toEqual(before);
    expect(keys([one, settleTimeout, undoStuck, three, two])).toEqual(before);
    expect(keys([three, settleTimeout, undoStuck, two])).toEqual(before);
    // A case that leaves takes only the group after it along.
    expect(keys([one, two])).toEqual([before[0]]);
    // Each group has its own, also after two cases.
    const other: IncidentView = {
      ...undoStuck,
      id: "2b7e4c19-0000-4000-8000-0000000000bb",
      held_by: { ...undoStuck.held_by!, id: "6d18b3f0-0000-4000-8000-bb" },
    };
    const many = keys([one, settleTimeout, undoStuck, two, other, three]);
    expect(many).toHaveLength(5);
    expect(new Set(many).size).toBe(5);
  });

  it("a case is a group of its own, and the incidents between two cases are one", () => {
    const [before, one, two] = ["a", "b", "c"].map((letter) =>
      plain(`${letter}0000000-0000-4000-8000-000000000001`),
    );
    const other: IncidentView = {
      ...undoStuck,
      id: "2b7e4c19-0000-4000-8000-0000000000bb",
      held_by: { ...undoStuck.held_by!, id: "6d18b3f0-0000-4000-8000-bb" },
    };
    const { high } = inboxRows(
      [before, settleTimeout, undoStuck, one, two, other],
      [],
    );
    expect(
      rowGroups(high).map((group) =>
        group.case
          ? group.case.members.map((item) => item.id)
          : group.items.map((item) => item.id),
      ),
    ).toEqual([
      [before.id],
      [settleTimeout.id, undoStuck.id],
      [one.id, two.id],
      [other.id],
    ]);
  });
});

describe("what a case says", () => {
  it("names the step that waits, without its undo, whichever incident leads", () => {
    const parentLed = onlyCase(inboxRows([settleTimeout, undoStuck], []).high);
    expect(caseLabel(parentLed)).toBe(
      "Case: move is waiting for a called run in doubt",
    );
    const childLed = onlyCase(inboxRows([undoStuck], []).high);
    expect(caseLabel(childLed)).toBe(
      "Case: move is waiting for a called run in doubt",
    );
    const suffixed: IncidentView = { ...settleTimeout, node_id: "move#undo" };
    expect(caseLabel(onlyCase(inboxRows([suffixed], []).high))).toBe(
      "Case: move is waiting for a called run in doubt",
    );
  });

  it("says where an incident that isn't in the list is", () => {
    expect(
      missingText({
        ...undoStuck.held_by!,
        run_id: "6d18b3f0-1a2b-4c3d-8e4f-5a6b7c8d9e0f",
      }),
    ).toBe("Not in this list · move in run 6d18b3f0");
    expect(
      linkPlace({ ...settleTimeout.waits_on![0], node_id: "add#undo" }),
    ).toBe("add in run 9a01c7d2");
    // A run held whole is tied to no step.
    for (const node_id of [null, "@transactions"])
      expect(linkPlace({ ...settleTimeout.waits_on![0], node_id })).toBe(
        "run 9a01c7d2",
      );
  });
});
