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
// Opening the inbox from a run, on an incident or on a code: the shell's page
// first, then the address, unless the person is elsewhere by then. And where a
// stopped run's notice leads, by its code and the transactions listed.
import { describe, expect, it } from "vitest";
import {
  noticeSteps,
  openIncident,
  showInIncidents,
} from "../src/app/operate/transactions/open-incident";
import type { RunTransactionView } from "../src/app/operate/transactions/transaction-contracts";

const incident = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d";
/** A shell whose page changes to `lands` once it is asked for the inbox. */
function shell(lands: string) {
  const steps: string[] = [];
  const host = {
    view: "runs",
    navigate: async (view: string) => {
      steps.push(`page ${view}`);
      await Promise.resolve();
      host.view = lands;
    },
  };
  const router = {
    navigateByUrl: (url: string, extras?: { replaceUrl?: boolean }) => {
      steps.push(`${extras?.replaceUrl ? "replace" : "push"} ${url}`);
      return Promise.resolve(true);
    },
  };
  return {
    steps,
    open: () =>
      openIncident(
        host as unknown as Parameters<typeof openIncident>[0],
        router as unknown as Parameters<typeof openIncident>[1],
        incident,
      ),
    show: (code: string) =>
      showInIncidents(
        host as unknown as Parameters<typeof showInIncidents>[0],
        router as unknown as Parameters<typeof showInIncidents>[1],
        code,
      ),
  };
}

describe("opening the inbox on an incident", () => {
  it("moves the page first, then names the incident without another step back", async () => {
    const { open, steps } = shell("incidents");
    await open();
    expect(steps).toEqual([
      "page incidents",
      `replace /operate/incidents?incident=${incident}`,
    ]);
  });

  it("leaves a person who is elsewhere by then where they are", async () => {
    const { open, steps } = shell("workers");
    await open();
    expect(steps).toEqual(["page incidents"]);
  });

  it("shows the incidents of a code, those of test runs too, in the address of the inbox's own filters", async () => {
    const there = shell("incidents");
    await there.show("WV-TXN-RUN-OVERSIZED");
    expect(there.steps).toEqual([
      "page incidents",
      "replace /operate/incidents?code=WV-TXN-RUN-OVERSIZED&include_test=true",
    ]);
    const elsewhere = shell("workers");
    await elsewhere.show("WV-TXN-RUN-OVERSIZED");
    expect(elsewhere.steps).toEqual(["page incidents"]);
  });
});

describe("where a stopped run's notice leads", () => {
  const view = (
    key: string,
    state: RunTransactionView["state"],
    incident_id?: string,
  ): RunTransactionView => ({
    instance_key: key,
    node_id: "move",
    iteration: [],
    state,
    opened_at: "2026-11-22T10:31:00Z",
    participants: [],
    ...(incident_id ? { incident_id } : {}),
  });
  const settled = view("move[1]", "completed");
  const doubt = view("move[2]", "in_doubt", incident);
  const other = view(
    "move[3]",
    "in_doubt",
    "b1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  );
  const steps = (
    views: RunTransactionView[],
    complete = true,
    code = "WV-TXN-UNDO-STUCK",
  ) => noticeSteps(code, { views, complete });

  it("to the incident and the transaction in doubt", () => {
    expect(steps([settled, doubt])).toEqual({
      open: { id: incident, label: "Open incident" },
      code: null,
      goTo: "Go to the transaction",
    });
  });

  it("to the first of several, and only once every one is listed", () => {
    expect(steps([settled, doubt, other])).toEqual({
      open: { id: incident, label: "Open first incident" },
      code: null,
      goTo: "Go to the first transaction in doubt",
    });
    expect(steps([settled, doubt, other], false)).toEqual({
      open: { id: incident, label: "Open incident" },
      code: null,
      goTo: "Go to the transaction",
    });
  });

  it("to the first incident that one of several names", () => {
    const unnamed = view("move[2]", "in_doubt");
    expect(steps([settled, unnamed, other])).toEqual({
      open: { id: other.incident_id, label: "Open first incident" },
      code: null,
      goTo: "Go to the first transaction in doubt",
    });
  });

  it.each([
    ["a run held whole, whose transactions name none", [settled], true],
    ["a transaction in doubt that lies past the pages read", [settled], false],
    ["transactions that couldn't be read, or be shown", [], false],
  ] as const)(
    "to the incidents of its own code, for %s",
    (_case, views, all) => {
      expect(steps([...views], all, "WV-TXN-RUN-OVERSIZED")).toEqual({
        open: null,
        code: "WV-TXN-RUN-OVERSIZED",
        goTo: null,
      });
    },
  );

  it("to the incidents of its code and to the transaction, when that one names no incident", () => {
    expect(steps([settled, view("move[2]", "in_doubt")])).toEqual({
      open: null,
      code: "WV-TXN-UNDO-STUCK",
      goTo: "Go to the transaction",
    });
  });

  it("nowhere until the transactions are read, or when no transaction stopped the run", () => {
    expect(noticeSteps("WV-TXN-UNDO-STUCK", null)).toBeNull();
    expect(steps([doubt], true, "WV-TASK-AMBIGUOUS")).toBeNull();
    expect(steps([doubt], true, "")).toBeNull();
  });
});
