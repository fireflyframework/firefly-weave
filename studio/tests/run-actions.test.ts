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
    notices: [] as { text: string; tone: string | undefined }[],
    failures: [] as unknown[],
    reads: 0,
    forms: 0,
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
    },
    can: () => true,
    runNodes: () => [],
    notify: (text: string, _action?: unknown, tone?: string) =>
      calls.notices.push({ text, tone }),
    fail: (error: unknown) => calls.failures.push(error),
    readDetail: async () => void calls.reads++,
    refresh: async () => undefined,
    viewRun: async () => undefined,
    dialogs: {
      form: async (): Promise<Json | null> => {
        calls.forms++;
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
  );
  actions.follow(open ?? "");
  const settle = () => new Promise((resolve) => setTimeout(resolve));
  return {
    host,
    calls,
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
