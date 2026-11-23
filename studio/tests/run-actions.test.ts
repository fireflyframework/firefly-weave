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
  RunActions,
  compareVersions,
  explainRefusal,
  historyFileName,
  isTerminal,
  payloadProblem,
  reasonProblem,
  retryChoices,
  retryInput,
  waitingSignal,
} from "../src/app/operate/runs/run-actions";
import type { App } from "../src/app/app";
import type { Node } from "../src/app/model";
import type { RunTransactionView } from "../src/app/operate/transactions/transaction-contracts";
import { typedConfirmation } from "../src/app/operate/transactions/transaction-record";

const activation = (id: string, version: string, extra = {}) => ({
  id,
  name: "expense-review",
  revision: 1,
  request: { version_id: `v-${version}` },
  ...extra,
});
const versions = new Map(
  ["1.0.0", "1.2.0", "1.10.0", "0.9.0"].map((version) => [
    `v-${version}`,
    { name: "expense-review", version },
  ]),
);
const run = {
  id: "5e6f7a8b-0000-4000-8000-000000000001",
  activation: activation("same", "1.0.0"),
  business_key: "order-7",
  state: { status: "failed", input: { amount: 42 }, active: ["approval"] },
};

describe("run commands", () => {
  it("knows terminal runs from the run's own status", () => {
    expect(isTerminal(run)).toBe(true);
    expect(isTerminal({ state: { status: "waiting" } })).toBe(false);
    expect(isTerminal({ state: { status: "cancelled" } })).toBe(true);
    expect(isTerminal({})).toBe(false);
  });

  it("offers the run's version and a newer active version to retry", () => {
    const choices = retryChoices(
      run,
      [
        activation("old", "0.9.0"),
        activation("newer", "1.2.0"),
        activation("newest", "1.10.0"),
        activation("retired", "2.0.0", { retired: true }),
        { ...activation("other", "3.0.0"), name: "other-workflow" },
      ],
      versions,
    );
    expect(choices!.activations.map((item) => item["id"])).toEqual([
      "same",
      "newest",
    ]);
    expect([...choices!.labels]).toEqual([
      ["same", "Same version (1.0.0)"],
      ["newest", "Latest active version (1.10.0)"],
    ]);
    expect(choices!.initial).toBe("same");
  });

  it("offers only the run's version when nothing newer is active", () => {
    const choices = retryChoices(run, [activation("old", "0.9.0")], versions);
    expect(choices!.activations.map((item) => item["id"])).toEqual(["same"]);
    expect(retryChoices({ id: "x" }, [], versions)).toBeNull();
  });

  it("starts a retry from the run's input object", () => {
    expect(retryInput(run)).toEqual({ amount: 42 });
    expect(retryInput({ state: { input: [1, 2] } })).toEqual({});
  });

  it("finds the signal a waiting run expects", () => {
    const nodes = [
      { step: { id: "approval", kind: "signal", name: "customer-approved" } },
      { step: { id: "review", kind: "humanTask" } },
    ] as unknown as Node[];
    expect(waitingSignal(run, nodes)).toBe("customer-approved");
    expect(waitingSignal({ state: { active: ["review"] } }, nodes)).toBe("");
  });

  it("checks reasons and payloads before sending", () => {
    expect(reasonProblem("x".repeat(2000))).toBe("");
    expect(reasonProblem("x".repeat(2001))).toBe(
      "Keep the reason to 2,000 characters or fewer.",
    );
    expect(payloadProblem('{"approved": true}')).toBe("");
    expect(payloadProblem("null")).toBe("");
    expect(payloadProblem("{approved}")).toBe(
      'Enter the payload as JSON, for example {"approved": true}.',
    );
  });

  it("compares versions by their numbers and names the export file", () => {
    expect(compareVersions("1.10.0", "1.9.0")).toBeGreaterThan(0);
    expect(compareVersions("1.0.0", "1.0.0-beta")).toBe(0);
    expect(compareVersions("0.9.0", "1.0.0")).toBeLessThan(0);
    expect(historyFileName(run.id)).toBe(
      "run-5e6f7a8b-0000-4000-8000-000000000001-history.json",
    );
  });

  it("names the capability a refused command needed, and keeps the code", () => {
    const refusal = (status: number, code: string, message: string) =>
      Object.assign(new Error(message), { plain: { status, code, message } });
    expect(
      explainRefusal(refusal(403, "WV-DENIED", "Denied"), "run.cancel"),
    ).toEqual({
      status: 403,
      code: "WV-DENIED",
      message: "You need run.cancel in this environment. Denied",
    });
    // Any other refusal keeps the platform's own words.
    expect(
      explainRefusal(
        refusal(409, "WV-RUNTIME-STATE", "Only a terminal run can be retried"),
        "run.retry",
      ),
    ).toEqual({
      status: 409,
      code: "WV-RUNTIME-STATE",
      message: "Only a terminal run can be retried",
    });
  });
});

// ------------------------------------------------ answers belong to their run

type Json = Record<string, unknown>;
const first = "5e6f7a8b-0000-4000-8000-000000000001";
const second = "9c8d7e6f-0000-4000-8000-000000000009";
const refused = (status: number, code: string, message: string) =>
  Object.assign(new Error(message), { plain: { status, code, message } });

/** A shell standing in for the app, with the answers held until released. */
function shell(open: string | null = first) {
  const calls = {
    sent: [] as { path: string; body: unknown }[],
    notices: [] as {
      text: string;
      tone: string | undefined;
      action?: string;
      run?: () => void;
    }[],
    failures: [] as unknown[],
    reads: 0,
    forms: 0,
    /** Each form the shell was asked to show. */
    asked: [] as Json[],
    /** Each runs.transactions read, by path. */
    transactionReads: [] as string[],
    /** Each address the actions opened. */
    opened: [] as string[],
  };
  /**
   * The run's transactions as the next read answers them; null fails the
   * read. With pages, each read answers the page its cursor names.
   */
  const platform: {
    transactions: RunTransactionView[] | null;
    pages?: RunTransactionView[][];
  } = {
    transactions: [],
  };
  let answer!: { resolve: (value: Json) => void; reject: (e: unknown) => void };
  const host = {
    view: "runs",
    identity: { principal_id: "human", grants: [] },
    selectedRecord: open
      ? ({
          id: open,
          activation: activation("same", "1.0.0"),
          state: { status: "waiting", input: { amount: 42 } },
        } as Json)
      : null,
    unknownCommand: false,
    workflowVersions: versions,
    exportFallback: null,
    api: {
      environment: "/env",
      page: async (
        _collection?: string,
        _environment?: boolean,
        _cursor?: string,
      ): Promise<{ items: Json[]; next_cursor: string | null }> => ({
        items: [],
        next_cursor: null,
      }),
      request: async <T>(path: string): Promise<T> => {
        calls.transactionReads.push(path);
        if (platform.transactions === null)
          throw refused(503, "WV-UNAVAILABLE", "Down");
        const pages = platform.pages ?? [platform.transactions];
        const cursor = new URL(path, "http://studio").searchParams.get(
          "cursor",
        );
        const at = cursor ? Number(cursor.replace("page-", "")) : 0;
        return {
          items: structuredClone(pages[at]),
          next_cursor: at + 1 < pages.length ? `page-${at + 1}` : null,
        } as T;
      },
    },
    can: (_capability: string, _resource?: string) => true,
    runNodes: () => [],
    notify: (
      text: string,
      action?: { label: string; run: () => void },
      tone?: string,
    ) =>
      calls.notices.push({
        text,
        tone,
        ...(action ? { action: action.label, run: action.run } : {}),
      }),
    fail: (error: unknown) => calls.failures.push(error),
    readDetail: async () => void calls.reads++,
    refreshRuns: async () => undefined,
    viewRun: async () => undefined,
    dialogs: {
      form: async (options: Json): Promise<Json | null> => {
        calls.forms++;
        calls.asked.push(options);
        return { reason: "Stop it" };
      },
    },
    safeMutation: (path: string, _method: string, body: unknown) => {
      calls.sent.push({ path, body });
      return new Promise<Json>(
        (resolve, reject) => (answer = { resolve, reject }),
      );
    },
  };
  const actions = new RunActions(
    () => host as unknown as App,
    () => undefined,
    (path) => calls.opened.push(path),
  );
  actions.follow(open ?? "");
  const settle = () => new Promise((resolve) => setTimeout(resolve));
  return {
    host,
    calls,
    platform,
    actions,
    settle,
    answer: (value: Json) => answer.resolve(value),
    refuse: (error: unknown) => answer.reject(error),
    /** The person opens another run, or closes this one. */
    moveTo: (id: string | null) => {
      host.selectedRecord = id ? { id, state: { status: "waiting" } } : null;
      actions.follow(id ?? "");
    },
  };
}

describe("a command's answer belongs to the run it was sent for", () => {
  it("cancels the run that was open and reads it again", async () => {
    const s = shell();
    const sending = s.actions.cancel();
    await s.settle();
    expect(s.calls.sent).toEqual([
      { path: `/env/runs/${first}/cancel`, body: { reason: "Stop it" } },
    ]);
    s.answer({});
    await sending;
    expect(s.calls.reads).toBe(1);
    expect(s.calls.notices.map((n) => n.text)).toEqual([
      "Run canceled. Steps that already started may still finish outside Weave.",
    ]);
  });

  it("only says what happened when the run was closed meanwhile", async () => {
    const s = shell();
    const sending = s.actions.cancel();
    await s.settle();
    s.moveTo(null);
    s.answer({});
    await sending;
    expect(s.calls.reads).toBe(0);
    expect(s.calls.notices.map((n) => n.text)).toEqual([
      "Run 5e6f7a8b canceled. Steps that already started may still finish outside Weave.",
    ]);
  });

  it("keeps a refusal for another run out of the run opened meanwhile", async () => {
    const s = shell();
    const sending = s.actions.cancel();
    await s.settle();
    s.moveTo(second);
    s.refuse(
      refused(409, "WV-RUNTIME-TERMINAL", "Historical runs cannot be canceled"),
    );
    await sending;
    expect(s.calls.failures).toEqual([]);
    expect(s.calls.notices).toEqual([
      {
        text: "Run 5e6f7a8b was not canceled. Historical runs cannot be canceled. Support code: WV-RUNTIME-TERMINAL.",
        tone: "danger",
      },
    ]);
  });

  it("shows a refusal of the open run where the person is looking", async () => {
    const s = shell();
    const sending = s.actions.cancel();
    await s.settle();
    s.refuse(refused(403, "WV-DENIED", "Denied"));
    await sending;
    expect(s.calls.notices).toEqual([]);
    expect(s.calls.failures).toEqual([
      {
        plain: {
          status: 403,
          code: "WV-DENIED",
          message: "You need run.cancel in this environment. Denied",
        },
      },
    ]);
  });

  it("sends nothing for a run the person left while its dialog was open", async () => {
    const s = shell();
    s.host.dialogs.form = async () => {
      s.calls.forms++;
      s.moveTo(second);
      return { reason: "Stop it" };
    };
    await s.actions.cancel();
    expect(s.calls.sent).toEqual([]);
    expect(s.calls.notices.map((n) => n.text)).toEqual([
      "Run 5e6f7a8b was not canceled: it is no longer open.",
    ]);
  });

  it("says a signal reached a run that is no longer open", async () => {
    const s = shell();
    s.host.dialogs.form = async () => ({
      name: "customer-approved",
      eventId: "event-1",
      payload: '{"approved": true}',
    });
    const sending = s.actions.signal();
    await s.settle();
    expect(s.calls.sent).toEqual([
      {
        path: `/env/runs/${first}/signals`,
        body: {
          eventId: "event-1",
          name: "customer-approved",
          payload: { approved: true },
        },
      },
    ]);
    s.moveTo(second);
    s.answer({});
    await sending;
    expect(s.calls.reads).toBe(0);
    expect(s.calls.notices.map((n) => n.text)).toEqual([
      "Signal sent to run 5e6f7a8b.",
    ]);
  });

  it("drops a Retry run dialog when another run is opened", async () => {
    const s = shell();
    s.host.selectedRecord = {
      ...s.host.selectedRecord,
      state: { status: "failed", input: { amount: 42 } },
    };
    await s.actions.openRetry();
    expect(s.actions.retry?.target.id).toBe(first);
    s.moveTo(second);
    expect(s.actions.retry).toBeNull();
  });

  it("keeps the answer to a retry out of the dialog opened for another run", async () => {
    const s = shell();
    const failed = (id: string) => ({
      id,
      activation: activation("same", "1.0.0"),
      state: { status: "failed", input: {} },
    });
    s.host.selectedRecord = failed(first);
    await s.actions.openRetry();
    const sending = s.actions.confirmRetry({
      activation_id: "same",
      input: {},
    });
    await s.settle();
    expect(s.calls.sent[0].path).toBe(`/env/runs/${first}/retry`);
    // Another run opens, and its own Retry run dialog with it.
    s.host.selectedRecord = failed(second);
    s.actions.follow(second);
    await s.actions.openRetry();
    const dialog = s.actions.retry;
    expect(dialog?.target.id).toBe(second);
    s.refuse(
      refused(409, "WV-RUNTIME-STATE", "Only a terminal run can be retried"),
    );
    await sending;
    expect(s.actions.retry).toBe(dialog);
    expect(s.actions.retry?.failure).toBeNull();
    expect(s.calls.notices[0].text).toContain("Run 5e6f7a8b was not retried.");
    expect(s.calls.notices[0].text).toContain("Support code: WV-RUNTIME-STATE");
  });

  it("keeps a refused retry in its own dialog, with the code", async () => {
    const s = shell();
    s.host.selectedRecord = {
      ...s.host.selectedRecord,
      state: { status: "failed", input: {} },
    };
    await s.actions.openRetry();
    const sending = s.actions.confirmRetry({
      activation_id: "same",
      input: {},
    });
    await s.settle();
    s.refuse(refused(403, "WV-DENIED", "Denied"));
    await sending;
    expect(s.actions.retry?.failure).toEqual({
      status: 403,
      code: "WV-DENIED",
      message: "You need run.retry in this environment. Denied",
    });
    expect(s.calls.notices).toEqual([]);
    expect(s.calls.failures).toEqual([]);
  });
});

describe("reading versions for a retry", () => {
  it("compares a retired original version using uncached metadata", async () => {
    const s = shell();
    s.host.selectedRecord = {
      ...run,
      activation: activation("same", "2.0.0", { retired: true }),
    };
    s.host.api.page = async (collection) => ({
      items:
        collection === "activations"
          ? [activation("older", "1.2.0")]
          : [{ id: "v-2.0.0", name: "expense-review", version: "2.0.0" }],
      next_cursor: null,
    });
    await s.actions.openRetry();
    expect(s.actions.retry?.activations.map((a) => a["id"])).toEqual(["same"]);
    expect(s.actions.retry?.labels.get("same")).toBe("Same version (2.0.0)");
  });

  it("offers only the original when its version cannot be compared", async () => {
    const s = shell();
    s.host.selectedRecord = { ...run, activation: activation("same", "2.0.0") };
    s.host.api.page = async (collection) => ({
      items: collection === "activations" ? [activation("older", "1.2.0")] : [],
      next_cursor: null,
    });
    await s.actions.openRetry();
    expect(s.actions.retry?.activations.map((a) => a["id"])).toEqual(["same"]);
    expect(s.actions.retry?.initial).toBe("same");
    expect(s.actions.retry?.notice).toContain(
      "could not check every active version",
    );
  });

  it("bounds missing version metadata and keeps the shell cache unchanged", async () => {
    const s = shell();
    s.host.selectedRecord = run;
    let workflowReads = 0;
    s.host.api.page = async (collection) => {
      if (collection === "activations")
        return {
          items: [activation("known", "1.2.0"), activation("unknown", "2.0.0")],
          next_cursor: null,
        };
      workflowReads++;
      return {
        items: [{ id: "unrelated", name: "other", version: "9.0.0" }],
        next_cursor: "more",
      };
    };
    await s.actions.openRetry();
    expect(workflowReads).toBe(10);
    expect(s.actions.retry?.labels.get("known")).toBe(
      "Newer active version (1.2.0)",
    );
    expect(s.actions.retry?.notice).toContain(
      "could not check every active version",
    );
    expect(s.host.workflowVersions.has("unrelated")).toBe(false);
  });

  for (const change of ["close", "identity", "environment", "destroy"])
    it(`stops retry catalog paging after ${change}`, async () => {
      const s = shell();
      s.host.selectedRecord = run;
      let release!: (value: {
        items: Json[];
        next_cursor: string | null;
      }) => void;
      let reads = 0;
      s.host.api.page = async () => {
        reads++;
        return new Promise((resolve) => (release = resolve));
      };
      const reading = s.actions.openRetry();
      await s.settle();
      if (change === "close") s.moveTo(null);
      if (change === "identity")
        s.host.identity = { principal_id: "other", grants: [] };
      if (change === "environment") s.host.api.environment = "/other";
      if (change === "destroy") s.actions.destroy();
      release({ items: [activation("new", "1.2.0")], next_cursor: "more" });
      await reading;
      expect(reads).toBe(1);
      expect(s.actions.retry).toBeNull();
    });
});

// ------------------------------------------------ a run with transactions

const view = (
  state: RunTransactionView["state"],
  participants: RunTransactionView["participants"],
  extra: Partial<RunTransactionView> = {},
): RunTransactionView => ({
  instance_key: "move",
  node_id: "move",
  iteration: [],
  state,
  opened_at: "2026-11-22T10:31:00Z",
  participants,
  ...extra,
});
const step = (
  key: string,
  mode: "reserve" | "compensate" | "irreversible",
  status: RunTransactionView["participants"][number]["status"],
) => ({
  node_id: key,
  instance_key: key,
  mode,
  status,
  effect: "none" as const,
});
const incident = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d";
/** A transaction still running: hold reserved, add running. */
const open = view("open", [
  step("hold", "reserve", "done"),
  step("add", "compensate", "dispatched"),
  step("submit", "irreversible", "pending"),
]);
const inDoubt = view(
  "in_doubt",
  [step("hold", "reserve", "done"), step("add", "compensate", "undo_stuck")],
  { instance_key: "move[2]", incident_id: incident },
);
const settled = view("completed", [step("hold", "reserve", "confirmed")], {
  instance_key: "move[1]",
});
const UNDO_FIRST =
  "This run has a transaction that isn't finished. Weave undoes what it did before the run ends. Give a reason for the audit log.";
const IN_DOUBT_LINE =
  "Cancel without undoing: a transaction is in doubt. Resolve it from its incident first.";

describe("Cancel run on a run with transactions", () => {
  it("reads the run's transactions first and says Weave undoes them before the run ends", async () => {
    const s = shell();
    s.platform.transactions = [settled, open];
    const sending = s.actions.cancel();
    await s.settle();
    expect(s.calls.transactionReads).toEqual([
      `/env/runs/${first}/transactions?limit=50`,
    ]);
    expect(s.calls.asked).toMatchObject([
      {
        title: "Cancel this run?",
        message: UNDO_FIRST,
        confirmLabel: "Cancel run",
        cancelLabel: "Keep running",
      },
    ]);
    s.answer({ id: first, pending_undo: true });
    await sending;
    expect(s.calls.notices.map((n) => n.text)).toEqual([
      "Canceling: undoing what this run's transactions did.",
    ]);
    expect(s.calls.reads).toBe(1);
  });

  it("keeps today's words when every transaction has settled, or none could be read", async () => {
    for (const transactions of [[settled], null]) {
      const s = shell();
      s.platform.transactions = transactions;
      const sending = s.actions.cancel();
      await s.settle();
      expect(s.calls.asked[0]?.["message"]).toBe(
        "Steps that already started may still finish outside Weave. Give a reason for the audit log.",
      );
      s.answer({});
      await sending;
      expect(s.calls.notices.map((n) => n.text)).toEqual([
        "Run canceled. Steps that already started may still finish outside Weave.",
      ]);
    }
  });

  it("with a transaction in doubt, says so before sending and sends nothing", async () => {
    const s = shell();
    s.platform.transactions = [open, inDoubt];
    await s.actions.cancel();
    expect(s.calls.forms).toBe(0);
    expect(s.calls.sent).toEqual([]);
    expect(s.actions.cancelDialog).toMatchObject({
      mode: "in_doubt",
      incidentId: incident,
    });
    s.actions.openIncident(incident);
    expect(s.calls.opened).toEqual([`/operate/incidents?incident=${incident}`]);
    s.actions.closeCancelDialog();
    expect(s.actions.cancelDialog).toBeNull();
  });

  it("finds a transaction in doubt on a later page, and sends nothing", async () => {
    const s = shell();
    s.platform.pages = [Array(50).fill(settled), [inDoubt]];
    const sending = s.actions.cancel();
    await s.settle();
    expect(s.calls.transactionReads).toEqual([
      `/env/runs/${first}/transactions?limit=50`,
      `/env/runs/${first}/transactions?limit=50&cursor=page-1`,
    ]);
    expect(s.calls.forms).toBe(0);
    expect(s.calls.sent).toEqual([]);
    expect(s.actions.cancelDialog).toMatchObject({
      mode: "in_doubt",
      incidentId: incident,
    });
    await sending;
  });

  it("says Weave undoes first when pages remain past the ones it reads", async () => {
    const s = shell();
    s.platform.pages = Array.from({ length: 11 }, () => [settled]);
    const sending = s.actions.cancel();
    await s.settle();
    expect(s.calls.transactionReads).toHaveLength(10);
    expect(s.calls.asked[0]?.["message"]).toBe(UNDO_FIRST);
    s.answer({});
    await sending;
  });

  it("a refusal because a transaction went in doubt meanwhile names it and offers its incident", async () => {
    const s = shell();
    s.platform.transactions = [open];
    const sending = s.actions.cancel();
    await s.settle();
    s.platform.transactions = [inDoubt];
    s.refuse(refused(409, "WV-TXN-IN-DOUBT", "Resolve the transaction first."));
    await sending;
    expect(s.calls.failures).toEqual([]);
    expect(s.calls.notices).toMatchObject([
      {
        text: "Run 5e6f7a8b was not canceled. Resolve the transaction first. Support code: WV-TXN-IN-DOUBT.",
        tone: "danger",
        action: "Open incident",
      },
    ]);
    s.calls.notices[0].run!();
    expect(s.calls.opened).toEqual([`/operate/incidents?incident=${incident}`]);
  });
});

describe("Cancel without undoing", () => {
  it("is offered only with run.cancel, transaction.reconcile and a transaction not settled", () => {
    const s = shell();
    s.actions.know(first, [open], true);
    expect(s.actions.withoutUndoing()).toEqual({ blocked: null });
    s.actions.know(first, [settled], true);
    expect(s.actions.withoutUndoing()).toBeNull();
    s.actions.know(first, [open], true);
    for (const missing of ["run.cancel", "transaction.reconcile"]) {
      s.host.can = (capability: string) => capability !== missing;
      expect(s.actions.withoutUndoing(), missing).toBeNull();
    }
    s.host.can = () => true;
    // Records known for another run say nothing about this one.
    s.actions.know(second, [open], true);
    expect(s.actions.withoutUndoing()).toBeNull();
    s.actions.know(first, [open], true);
    s.host.selectedRecord = { id: first, state: { status: "cancelled" } };
    expect(s.actions.withoutUndoing()).toBeNull();
  });

  it("stays in the row while a transaction is in doubt, saying why and which incident to resolve", () => {
    const s = shell();
    s.actions.know(first, [open, inDoubt], true);
    expect(s.actions.withoutUndoing()).toEqual({
      blocked: { text: IN_DOUBT_LINE, incidentId: incident },
    });
  });

  /** The section's first page: 50 transactions that completed. */
  const firstPage = Array.from({ length: 50 }, (_, index) => ({
    ...settled,
    instance_key: `move[${index + 100}]`,
  }));
  const later = view("open", [step("ship", "compensate", "done")], {
    instance_key: "move[155]",
  });

  it("is offered while only the first page is known, and the read of every page decides", async () => {
    const s = shell();
    s.actions.know(first, firstPage, false);
    expect(s.actions.withoutUndoing()).toEqual({ blocked: null });
    s.platform.pages = [firstPage, [later]];
    await s.actions.cancelWithoutUndoing();
    expect(s.calls.transactionReads).toHaveLength(2);
    expect(
      s.actions.cancelDialog!.lines.map(
        (line) => `${line.step} · ${line.how}: ${line.what}`,
      ),
    ).toEqual(["ship · Reversible: Weave won't undo it"]);
  });

  it("keeps what a read of every page found over the first page read again", async () => {
    const s = shell();
    s.actions.know(first, firstPage, false);
    s.platform.pages = [firstPage, [inDoubt]];
    await s.actions.cancelWithoutUndoing();
    expect(s.actions.withoutUndoing()?.blocked?.incidentId).toBe(incident);
    // The section reads its first page again: the transaction in doubt on
    // the second page still holds the button back.
    s.actions.know(first, firstPage, false);
    expect(s.actions.withoutUndoing()).toEqual({
      blocked: { text: IN_DOUBT_LINE, incidentId: incident },
    });
  });

  it("takes newer records from the first page, and offers it again once the run has more", async () => {
    const s = shell();
    const hold = { ...open, instance_key: "move[5]" };
    s.actions.know(first, [hold], true);
    s.platform.transactions = [hold, settled];
    await s.actions.cancelWithoutUndoing();
    s.actions.closeCancelDialog();
    // move[5] completed since: nothing the reads know is left unsettled.
    const done = { ...settled, instance_key: "move[5]" };
    s.actions.know(first, [done, settled], false);
    expect(s.actions.withoutUndoing()).toBeNull();
    // A transaction the read of every page didn't hold: more may follow it.
    s.actions.know(
      first,
      [done, settled, { ...settled, instance_key: "move[3]" }],
      false,
    );
    expect(s.actions.withoutUndoing()).toEqual({ blocked: null });
  });

  it("reads the transactions again, lists what stays done, and sends skip_undo once", async () => {
    const s = shell();
    s.actions.know(first, [open], true);
    s.platform.transactions = [settled, open];
    await s.actions.cancelWithoutUndoing();
    expect(s.calls.transactionReads).toHaveLength(1);
    expect(s.actions.cancelDialog).toMatchObject({ mode: "without_undo" });
    expect(
      s.actions.cancelDialog!.lines.map(
        (line) => `${line.step} · ${line.how}: ${line.what}`,
      ),
    ).toEqual([
      "hold · Reserved: Weave won't release it",
      "add · Reversible: still running and may finish; Weave won't undo it",
    ]);
    const sending = s.actions.confirmWithoutUndoing(
      "The target system is down",
    );
    void s.actions.confirmWithoutUndoing("The target system is down");
    await s.settle();
    expect(s.calls.sent).toEqual([
      {
        path: `/env/runs/${first}/cancel`,
        body: { reason: "The target system is down", skip_undo: true },
      },
    ]);
    expect(s.actions.cancelDialog?.sending).toBe(true);
    s.answer({ id: first });
    await sending;
    expect(s.actions.cancelDialog).toBeNull();
    expect(s.calls.reads).toBe(1);
    expect(s.calls.notices.map((n) => n.text)).toEqual([
      "Run canceled without undoing. Steps already done stay done in other systems.",
    ]);
  });

  it("lists what stays done from every page of the run's transactions", async () => {
    const s = shell();
    s.actions.know(first, [open], true);
    const later = view("open", [step("ship", "compensate", "done")], {
      instance_key: "move[3]",
    });
    s.platform.pages = [[open], [later]];
    await s.actions.cancelWithoutUndoing();
    expect(s.calls.transactionReads).toHaveLength(2);
    expect(
      s.actions.cancelDialog!.lines.map(
        (line) => `${line.step} · ${line.how}: ${line.what}`,
      ),
    ).toEqual([
      "hold · Reserved: Weave won't release it",
      "add · Reversible: still running and may finish; Weave won't undo it",
      "ship · Reversible: Weave won't undo it",
    ]);
  });

  it("refuses in Studio when pages remain past the ones it reads, and sends nothing", async () => {
    const s = shell();
    s.actions.know(first, [open], true);
    s.platform.pages = Array.from({ length: 11 }, () => [open]);
    await s.actions.cancelWithoutUndoing();
    expect(s.calls.transactionReads).toHaveLength(10);
    expect(s.actions.cancelDialog).toBeNull();
    expect(s.calls.forms).toBe(0);
    expect(s.calls.sent).toEqual([]);
    expect(s.calls.notices).toEqual([
      {
        text: "Run 5e6f7a8b was not canceled. Studio couldn't read all of its transactions, so it can't list what stays done.",
        tone: "danger",
      },
    ]);
  });

  it("refuses in Studio when a transaction went in doubt since the row was drawn", async () => {
    const s = shell();
    s.actions.know(first, [open], true);
    s.platform.transactions = [inDoubt];
    await s.actions.cancelWithoutUndoing();
    expect(s.actions.cancelDialog).toBeNull();
    expect(s.calls.sent).toEqual([]);
    expect(s.calls.notices).toMatchObject([
      { text: IN_DOUBT_LINE, tone: "danger", action: "Open incident" },
    ]);
    // The row now knows it, and says so itself.
    expect(s.actions.withoutUndoing()?.blocked?.incidentId).toBe(incident);
  });

  it("names the server's refusal of a transaction in doubt, and keeps a refusal of access as today", async () => {
    const s = shell();
    s.actions.know(first, [open], true);
    s.platform.transactions = [open];
    await s.actions.cancelWithoutUndoing();
    let sending = s.actions.confirmWithoutUndoing("Down");
    await s.settle();
    s.platform.transactions = [inDoubt];
    s.refuse(refused(409, "WV-TXN-IN-DOUBT", "Resolve the transaction first."));
    await sending;
    expect(s.actions.cancelDialog).toBeNull();
    expect(s.calls.notices).toMatchObject([
      {
        text: "Run 5e6f7a8b was not canceled. Resolve the transaction first. Support code: WV-TXN-IN-DOUBT.",
        tone: "danger",
        action: "Open incident",
      },
    ]);
    s.platform.transactions = [open];
    await s.actions.cancelWithoutUndoing();
    sending = s.actions.confirmWithoutUndoing("Down");
    await s.settle();
    s.refuse(refused(403, "WV-FORBIDDEN", "Forbidden"));
    await sending;
    expect(s.calls.failures).toEqual([
      {
        plain: {
          status: 403,
          code: "WV-FORBIDDEN",
          message:
            "You need run.cancel and transaction.reconcile in this environment. Forbidden",
        },
      },
    ]);
  });

  it("says what happened to a run the person left, and changes nothing on screen", async () => {
    const s = shell();
    s.actions.know(first, [open], true);
    s.platform.transactions = [open];
    await s.actions.cancelWithoutUndoing();
    const sending = s.actions.confirmWithoutUndoing("Down");
    await s.settle();
    s.moveTo(second);
    expect(s.actions.cancelDialog).toBeNull();
    s.answer({ id: first });
    await sending;
    expect(s.calls.reads).toBe(0);
    expect(s.actions.cancelDialog).toBeNull();
    expect(s.calls.notices.map((n) => n.text)).toEqual([
      "Run 5e6f7a8b canceled without undoing. Steps already done stay done in other systems.",
    ]);
  });

  it("confirms only the run's short ID, whatever its case and spaces", () => {
    expect(typedConfirmation(first, "5e6f7a8b")).toBe(true);
    expect(typedConfirmation(first, " 5E6F 7A8B ")).toBe(true);
    expect(typedConfirmation(first, "5e6f7a8")).toBe(false);
    expect(typedConfirmation(first, first)).toBe(false);
    expect(typedConfirmation(first, "")).toBe(false);
  });
});
