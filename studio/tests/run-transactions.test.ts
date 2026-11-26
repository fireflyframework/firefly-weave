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
// A recorded run's transactions on the Runs tab: one page of runs.transactions
// read with a back-off, the same words a simulated run shows for the same
// transactions, the header and list captions of a run in doubt, and never a
// business key.
import { execFileSync } from "node:child_process";
import { beforeAll, describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import {
  inDoubtCaption,
  inDoubtLines,
  readRunTransactions,
  TransactionReadBackoff,
  undoneLine,
  type RunTransactionView,
} from "../src/app/editor/runs-tab/run-transactions";
import { chipName, itemWord } from "../src/app/editor/canvas/item-chip";
import {
  overlayOf,
  recordedLoops,
} from "../src/app/editor/runs-tab/run-overlay";
import { RunData } from "../src/app/editor/runs-tab/run-data";
import { readRunRecord } from "../src/app/editor/runs-tab/run-record";
import { runRow } from "../src/app/editor/runs-tab/runs-list-model";
import type { Workflow } from "../src/app/model";
import type { StepFact } from "../src/app/operate/run-contracts";
import type { ExecuteResponse } from "../src/app/editor/state/execution";
import {
  frameState,
  inDoubtHeader,
  recordedRollback,
  simulatedRollback,
  stepState,
  type TransactionRun,
} from "../src/app/editor/transactions/rollback";
import type { RunSummary } from "../src/app/operate/run-contracts";
import {
  fixtureIds,
  runViewsFixture,
} from "../src/app/operate/run-views.fixture";
import { python, pythonAvailable, repository } from "./python-path";
import { EXECUTIONS, executed } from "./transaction-answers";

const AT = "2026-11-25T10:00:00Z";
const INCIDENT = "0f6c2a9e-41b7-4c3a-9d8e-2b7f5a1c3d4e";
/** A value the platform recorded as the business key, which Studio never shows. */
const SECRET_KEY = "bk-7731-never-shown";
const part = (
  node: string,
  mode: "reserve" | "compensate" | "irreversible",
  status: string,
  extra: object = {},
) => ({
  node_id: node,
  instance_key: node,
  mode,
  status,
  effect: "unknown",
  ...extra,
});
const view = (
  state: string,
  participants: object[],
  extra: object = {},
): RunTransactionView =>
  ({
    instance_key: "move",
    node_id: "move",
    iteration: [],
    state,
    business_key: SECRET_KEY,
    opened_at: AT,
    participants,
    ...extra,
  }) as unknown as RunTransactionView;
const REFUSED = {
  code: "REQUEST_REFUSED",
  instance_key: "submit",
  effect: "none",
};
export const VIEWS = {
  undone: view(
    "compensated",
    [
      part("hold", "reserve", "cancelled"),
      part("add", "compensate", "undone"),
      part("submit", "irreversible", "failed", { effect: "none" }),
    ],
    { cause: REFUSED, decided_at: AT, ended_at: AT },
  ),
  stuck: view(
    "in_doubt",
    [
      part("hold", "reserve", "done"),
      part("add", "compensate", "undo_stuck", { attempts: 10 }),
      part("submit", "irreversible", "failed", { effect: "none" }),
    ],
    { cause: REFUSED, decided_at: AT, incident_id: INCIDENT },
  ),
  releaseStuck: view(
    "in_doubt",
    [
      part("hold", "reserve", "cancel_stuck", { attempts: 10 }),
      part("add", "compensate", "undone"),
      part("submit", "irreversible", "failed", { effect: "none" }),
    ],
    { cause: REFUSED, decided_at: AT, incident_id: INCIDENT },
  ),
  confirmStuck: view(
    "in_doubt",
    [
      part("hold", "reserve", "confirm_stuck", { attempts: 10 }),
      part("add", "compensate", "done"),
      part("submit", "irreversible", "done"),
    ],
    { decided_at: AT, incident_id: INCIDENT },
  ),
  // A step with nothing to undo failed: the participants don't say which incident.
  unexplained: view(
    "in_doubt",
    [part("hold", "reserve", "done"), part("add", "compensate", "done")],
    {
      cause: { code: "NOT_ALLOWED", instance_key: "check", effect: "unknown" },
      incident_id: INCIDENT,
    },
  ),
  inLoop: view(
    "in_doubt",
    [
      part("hold", "reserve", "done", { instance_key: "hold[2]" }),
      part("add", "compensate", "undo_stuck", {
        instance_key: "add[2]",
        attempts: 10,
      }),
      part("submit", "irreversible", "failed", { instance_key: "submit[2]" }),
    ],
    {
      instance_key: "move[2]",
      iteration: [2],
      cause: { ...REFUSED, instance_key: "submit[2]" },
      incident_id: INCIDENT,
    },
  ),
} satisfies Record<string, RunTransactionView>;

/** What a step's tile and a frame's header say, as the canvas reads them once the run ended. */
const words = (run: TransactionRun, ids: readonly string[]) =>
  Object.fromEntries(
    ids.map((id) => [
      id,
      (frameState(run, id, false) ?? stepState(run, id, false))?.text ?? null,
    ]),
  );
const tones = (run: TransactionRun, ids: readonly string[]) =>
  Object.fromEntries(
    ids.map((id) => [
      id,
      (frameState(run, id, false) ?? stepState(run, id, false))?.tone ?? null,
    ]),
  );
const IDS = ["move", "hold", "add", "submit"] as const;

/** A platform that answers runs.transactions with `answer`, and the paths asked. */
function platform(answer: () => unknown) {
  const asked: string[] = [];
  return {
    asked,
    api: {
      environment: "/v1/tenants/t/projects/p/environments/e",
      request: async <T>(path: string): Promise<T> => {
        asked.push(path);
        return answer() as T;
      },
    },
  };
}
const refusal = (status: number, code: string) => () => {
  throw new ApiError(status, { code, message: "No." });
};

describe("reading a run's transactions", () => {
  it("reads one page of 100 and says when more follow", async () => {
    const { api, asked } = platform(() => ({
      items: [VIEWS.undone],
      next_cursor: "c2",
    }));
    const read = await readRunTransactions(api, "run/1");
    expect(asked).toEqual([
      "/v1/tenants/t/projects/p/environments/e/runs/run%2F1/transactions?limit=100",
    ]);
    expect(read).toMatchObject({ more: true });
    expect("views" in read && read.views.map((one) => one.state)).toEqual([
      "compensated",
    ]);
    // A page that ends early ends the transactions all the same.
    const last = await readRunTransactions(
      platform(() => ({ items: [], next_cursor: null })).api,
      "r",
    );
    expect(last).toEqual({ views: [], more: false });
  });

  it("never keeps a business key, nor how a person resolved the transaction", async () => {
    const resolved = {
      ...VIEWS.undone,
      resolution: { kind: "terminate", reason: "Customer asked" },
    };
    const read = await readRunTransactions(
      platform(() => ({ items: [resolved], next_cursor: null })).api,
      "r",
    );
    const kept = JSON.stringify(read);
    expect(kept).not.toContain(SECRET_KEY);
    expect(kept).not.toContain("Customer asked");
    expect("views" in read && read.views[0].participants).toHaveLength(3);
  });

  it("is refused without run.read, and missing on a platform without it", async () => {
    expect(
      await readRunTransactions(
        platform(refusal(403, "WV-FORBIDDEN")).api,
        "r",
      ),
    ).toEqual({ refused: "forbidden" });
    expect(
      await readRunTransactions(
        platform(refusal(404, "WV-NOT-FOUND")).api,
        "r",
      ),
    ).toEqual({ refused: "missing" });
    await expect(
      readRunTransactions(platform(refusal(503, "WV-UNAVAILABLE")).api, "r"),
    ).rejects.toThrow();
  });

  it("leaves out a transaction in a state Studio doesn't know", async () => {
    const read = await readRunTransactions(
      platform(() => ({
        items: [VIEWS.undone, { ...VIEWS.undone, state: "paused_forever" }, 7],
        next_cursor: null,
      })).api,
      "r",
    );
    expect("views" in read && read.views).toHaveLength(1);
  });

  it("backs off 5 s after a failed read, then 10 s, and reads at once after one that worked", () => {
    const backoff = new TransactionReadBackoff();
    expect(backoff.ready(0)).toBe(true);
    backoff.failed(1_000);
    expect(backoff.ready(5_999)).toBe(false);
    expect(backoff.ready(6_000)).toBe(true);
    backoff.failed(6_000);
    expect(backoff.ready(15_999)).toBe(false);
    expect(backoff.ready(16_000)).toBe(true);
    // It never waits more than a minute.
    for (let at = 16_000; at < 1_000_000; at += 100_000) backoff.failed(at);
    expect(backoff.ready(916_000 + 59_999)).toBe(false);
    expect(backoff.ready(916_000 + 60_000)).toBe(true);
    backoff.succeeded();
    expect(backoff.ready(916_001)).toBe(true);
  });
});

describe("a recorded run's transactions on the canvas", () => {
  it("reads each frame and step as a simulated run does", () => {
    expect(words(recordedRollback([VIEWS.undone]), IDS)).toEqual({
      move: "Undone",
      hold: "Released",
      add: "Undone",
      submit: "Failed",
    });
  });

  it("reads a step still applied in a transaction left in doubt by how it's undone, never Done", () => {
    const run = recordedRollback([VIEWS.stuck]);
    expect(words(run, IDS)).toEqual({
      move: "In doubt · action needed",
      hold: "Still reserved",
      add: "Undo failed",
      submit: "Failed",
    });
    expect(tones(run, IDS)).toMatchObject({ move: "danger", hold: "warning" });
  });

  it("keeps Done with its success for steps of a transaction that went ahead", () => {
    const run = recordedRollback([VIEWS.confirmStuck]);
    expect(words(run, IDS)).toEqual({
      move: "In doubt · action needed",
      hold: "Confirm failed",
      add: "Done",
      submit: "Done",
    });
    expect(tones(run, ["add", "submit"])).toEqual({
      add: "success",
      submit: "success",
    });
  });
});

describe("a recorded loop with a transaction per item", () => {
  const steps = (id: string, extra: object = {}) => ({
    id,
    kind: "action",
    uses: `${id}.run@1.0.0`,
    ...extra,
  });
  const looped = {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "move-between-systems", version: "1.4.0" },
    spec: {
      steps: [
        {
          id: "each",
          kind: "forEach",
          items: { ref: "/input/items" },
          body: {
            steps: [
              {
                id: "move",
                kind: "transaction",
                onError: "continue",
                steps: [
                  steps("add", { compensate: steps("remove") }),
                  steps("submit", { irreversible: true }),
                ],
                output: { literal: null },
              },
            ],
            output: { literal: null },
          },
        },
      ],
    },
  } as unknown as Workflow;
  const record = readRunRecord({
    id: "3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b",
    activation: {
      request: { version_id: "9e8f5b0c-cfee-4af4-8dea-d2ae9fb06b9a" },
    },
    state: {
      status: "succeeded",
      active: [],
      input: { items: [1, 2, 3] },
      steps: {},
      waits: {},
      branches: {},
      incidents: {},
      loops: { each: { count: 3 } },
    },
  })!;
  const item = (index: number, state: string, add: string, submit: string) =>
    view(
      state,
      [
        part("add", "compensate", add, { instance_key: `add[${index}]` }),
        part("submit", "irreversible", submit, {
          instance_key: `submit[${index}]`,
        }),
      ],
      { instance_key: `move[${index}]`, iteration: [index] },
    );
  const VIEWS_PER_ITEM = [
    item(0, "completed", "done", "done"),
    item(1, "compensated", "undone", "failed"),
    item(2, "completed", "done", "done"),
  ];
  const facts = [0, 1, 2].map((index) => ({
    node_id: "submit",
    instance_key: `submit[${index}]`,
    iteration: [index],
    kind: "action",
    status: index === 1 ? "failed" : "succeeded",
    scheduled_at: null,
    started_at: null,
    ended_at: null,
    duration_ms: null,
    queue_wait_ms: null,
    attempts: 1,
    worker_id: null,
    error_code: index === 1 ? "REQUEST_REFUSED" : null,
    child_run_id: null,
    log_entries: 0,
    ...(index === 1 ? { handled: "continue" } : {}),
  })) as StepFact[];

  it("the item chip lists an item whose transaction was undone as Undone", () => {
    const [loop] = recordedLoops(looped, record, facts, VIEWS_PER_ITEM);
    expect(loop.items.map((one) => one.transaction ?? null)).toEqual([
      null,
      "Undone",
      null,
    ]);
    expect(chipName(1, loop)).toBe("Item 2 of 3, Undone. Choose an item");
    expect(chipName(0, loop)).toBe("Item 1 of 3, Finished. Choose an item");
    expect(itemWord(loop.items[1])).toBe("Undone");
    // Without the transactions, it says what its steps did.
    const [plain] = recordedLoops(looped, record, facts);
    expect(itemWord(plain.items[1])).toBe("Continued");
  });

  it("draws the transaction of the item picked", () => {
    const at = (pick: number) =>
      frameState(
        overlayOf(
          record,
          facts,
          { run: looped, draft: looped, runVersion: "1.4.0", test: false },
          undefined,
          { each: pick },
          VIEWS_PER_ITEM,
        ).canvas.transactions!,
        "move",
        false,
      )?.text;
    expect(at(1)).toBe("Undone");
    expect(at(0)).toBe("Completed");
  });
});

describe("step details of a recorded transaction", () => {
  const codes: Record<string, string> = {
    "add#undo": "UNAVAILABLE",
    "add[2]#undo": "UNAVAILABLE",
  };
  const codeOf = (key: string) => codes[key] ?? null;

  it("says which undo step failed, what is still applied, and opens its incident", () => {
    expect(inDoubtLines(VIEWS.stuck, codeOf)).toEqual({
      title:
        "This transaction is in doubt. Someone must resolve it in Operate.",
      detail:
        "Undo of add failed after 10 tries: UNAVAILABLE. Weave stopped undoing, so hold is still reserved.",
      result: "No result: the run stopped while move was in doubt.",
      incident: INCIDENT,
    });
    expect(inDoubtLines(VIEWS.releaseStuck, codeOf)?.detail).toBe(
      "Release of hold failed after 10 tries.",
    );
    expect(inDoubtLines(VIEWS.confirmStuck, codeOf)?.detail).toBe(
      "Confirm of hold failed after 10 tries.",
    );
    expect(inDoubtLines(VIEWS.inLoop, codeOf)?.detail).toBe(
      "Undo of add failed after 10 tries: UNAVAILABLE. Weave stopped undoing, so hold is still reserved.",
    );
    // Steps a person must look at: each named whole.
    const two = view(
      "in_doubt",
      [
        part("hold", "reserve", "done"),
        part("lock", "reserve", "done"),
        part("add", "compensate", "done"),
        part("copy", "compensate", "undo_stuck", { attempts: 1 }),
      ],
      { incident_id: INCIDENT },
    );
    expect(inDoubtLines(two, () => null)?.detail).toBe(
      "Undo of copy failed. Weave stopped undoing, so hold and lock are still reserved and add isn't undone.",
    );
    expect(inDoubtLines(VIEWS.undone, codeOf)).toBeNull();
  });

  it("are what the run's data gives the transaction and each of its steps", () => {
    const version = {
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "move-between-systems", version: "1.4.0" },
      spec: {
        steps: [
          {
            id: "move",
            kind: "transaction",
            policy: {
              holdSeconds: 900,
              undoRetry: {
                maxAttempts: 5,
                initialDelaySeconds: 2,
                maxDelaySeconds: 300,
              },
              undoTimeoutSeconds: 86_400,
            },
            steps: [],
            output: { literal: null },
          },
        ],
      },
    } as unknown as Workflow;
    const record = readRunRecord({
      id: "3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b",
      activation: {
        request: { version_id: "9e8f5b0c-cfee-4af4-8dea-d2ae9fb06b9a" },
      },
      state: {
        status: "suspended",
        active: [],
        input: {},
        steps: {},
        waits: {},
        branches: {},
        incidents: {},
      },
    })!;
    const data = new RunData(record, "env", version);
    data.setFacts([
      {
        node_id: "add",
        instance_key: "add#undo",
        iteration: [],
        kind: "action",
        status: "failed",
        scheduled_at: null,
        started_at: null,
        ended_at: null,
        duration_ms: null,
        queue_wait_ms: null,
        attempts: 10,
        worker_id: null,
        error_code: "UNAVAILABLE",
        child_run_id: null,
        log_entries: 0,
      },
    ]);
    const before = data.version();
    data.setTransactions([
      VIEWS.stuck,
      // Another item of the same transaction, in a loop around it.
      view(
        "compensated",
        [
          part("lift", "compensate", "undone", {
            instance_key: "lift[1]",
            attempts: 2,
          }),
        ],
        { instance_key: "move[1]", iteration: [1] },
      ),
    ]);
    expect(data.version()).toBeGreaterThan(before);
    expect(data.transaction("move", "move")).toEqual({
      kind: "in-doubt",
      pill: "In doubt · action needed",
      title:
        "This transaction is in doubt. Someone must resolve it in Operate.",
      detail:
        "Undo of add failed after 10 tries: UNAVAILABLE. Weave stopped undoing, so hold is still reserved.",
      result: "No result: the run stopped while move was in doubt.",
      incident: INCIDENT,
    });
    // Max tries of the version the run used.
    expect(data.transaction("lift", "lift[1]")).toEqual({
      kind: "undone",
      line: "This step was undone after the transaction failed (try 2 of 5).",
    });
    // That item's transaction was undone, not left in doubt.
    expect(data.transaction("move", "move[1]")).toBeNull();
    expect(data.transaction("hold", "hold")).toBeNull();
    expect(data.transaction("other", "other")).toBeNull();
    // Forgotten when the run's transactions can't be read any more.
    data.setTransactions([]);
    expect(data.transaction("move", "move")).toBeNull();
  });

  it("says a step of the transaction was undone or released, with its try", () => {
    const add = (status: string, attempts?: number) =>
      part("add", "compensate", status, attempts ? { attempts } : {}) as never;
    expect(undoneLine(add("undone", 2), 10)).toBe(
      "This step was undone after the transaction failed (try 2 of 10).",
    );
    expect(undoneLine(add("undone", 1), 10)).toBe(
      "This step was undone after the transaction failed.",
    );
    expect(undoneLine(part("hold", "reserve", "cancelled") as never, 10)).toBe(
      "This step's reservation was released after the transaction failed.",
    );
    expect(undoneLine(add("done"), 10)).toBeNull();
  });
});

describe("the run header of a run in doubt", () => {
  it("test_the_run_header_names_the_failed_step_and_the_transaction_in_doubt", () => {
    expect(inDoubtHeader([VIEWS.undone, VIEWS.stuck])).toBe(
      "Failed at submit: REQUEST_REFUSED · transaction move is in doubt · WV-TXN-UNDO-STUCK",
    );
    expect(inDoubtHeader([VIEWS.releaseStuck])).toBe(
      "Failed at submit: REQUEST_REFUSED · transaction move is in doubt · WV-TXN-CANCEL-STUCK",
    );
    expect(inDoubtHeader([VIEWS.confirmStuck])).toBe(
      "Transaction move is in doubt · WV-TXN-CONFIRM-STUCK",
    );
    // A cause the participants can't explain: no code.
    expect(inDoubtHeader([VIEWS.unexplained])).toBe(
      "Failed at check: NOT_ALLOWED · transaction move is in doubt",
    );
    // Inside a loop the step reads as the run header names it.
    expect(inDoubtHeader([VIEWS.inLoop])).toBe(
      "Failed at submit, item 3: REQUEST_REFUSED · transaction move is in doubt · WV-TXN-UNDO-STUCK",
    );
    expect(inDoubtHeader([VIEWS.undone])).toBeNull();
    expect(inDoubtHeader([])).toBeNull();
  });
});

describe("the Runs list row of a run in doubt", () => {
  const listed = (over: Partial<RunSummary> = {}): RunSummary => ({
    ...(runViewsFixture.runs.find(
      (run) => run.summary.id === fixtureIds.failed,
    )!.summary as RunSummary),
    status: "suspended",
    failed_step: null,
    ended_at: null,
    duration_ms: null,
    ...over,
  });
  const now = new Date("2026-10-07T12:00:00Z");

  it("test_the_runs_list_row_names_the_transaction_in_doubt", () => {
    const summary = listed({ transaction_state: "in_doubt" });
    expect(
      runRow(summary, now, inDoubtCaption([VIEWS.stuck])).transaction,
    ).toBe("Failed at submit · move in doubt");
    expect(runRow(summary, now).transaction).toBe("Transaction in doubt");
    expect(
      runRow(listed({ transaction_state: "compensated" }), now).transaction,
    ).toBeNull();
    // A summary without the field reads as it always did.
    const before = runRow(listed(), now);
    expect(before.transaction).toBeNull();
    const { transaction: _, ...rest } = runRow(
      listed({ transaction_state: "in_doubt" }),
      now,
    );
    const { transaction: __, ...today } = before;
    expect(rest).toStrictEqual(today);
  });
});

/** Snake case, as runs.transactions answers what the host's Execute says in camel case. */
function asRecorded(answer: ExecuteResponse): RunTransactionView[] {
  return (answer.transactions ?? []).map((one) => ({
    instance_key: one.instanceKey,
    node_id: one.nodeId,
    iteration: [...one.instanceKey.matchAll(/\[(\d+)\]/g)].map((m) =>
      Number(m[1]),
    ),
    state: one.state,
    opened_at: AT,
    participants: one.participants.map((p) => ({
      node_id: p.nodeId,
      instance_key: p.instanceKey,
      mode: p.mode,
      status: p.status,
      effect: p.effect,
      ...(p.attempts === undefined || p.attempts === null
        ? {}
        : { attempts: p.attempts }),
    })),
    ...(one.cause
      ? {
          cause: {
            code: one.cause.code,
            instance_key: one.cause.instanceKey,
            effect: one.cause.effect,
          },
        }
      : {}),
  })) as RunTransactionView[];
}

const PAGE = `
import json, sys
from firefly_weave.contracts.run_views import RunTransactionView
from firefly_weave.contracts.public import Page
for page in json.loads(sys.stdin.buffer.read().decode("utf-8")):
    Page[RunTransactionView].model_validate_json(json.dumps(page))
print("ok")
`;
const SAME = [
  "undone",
  "stuck",
  "unknown",
  "confirmStuck",
  "completed",
  "inLoop",
] as const;
const hasPython = pythonAvailable();
describe.skipIf(!hasPython)("recorded and simulated transactions", () => {
  let answers: Record<(typeof SAME)[number], ExecuteResponse>;
  beforeAll(() => {
    answers = executed(
      Object.fromEntries(SAME.map((name) => [name, EXECUTIONS[name]])),
    ) as typeof answers;
  }, 300_000);

  it("the views here are runs.transactions pages the platform could send", () => {
    const pages = [
      { items: Object.values(VIEWS), next_cursor: null },
      ...SAME.map((name) => ({
        items: asRecorded(answers[name]),
        next_cursor: null,
      })),
    ];
    expect(
      execFileSync(python, ["-c", PAGE], {
        cwd: repository,
        input: JSON.stringify(pages),
        encoding: "utf8",
        timeout: 180_000,
      }).trim(),
    ).toBe("ok");
  });

  it("read the same words for the same transactions", () => {
    for (const name of SAME) {
      const answer = answers[name];
      const simulated = simulatedRollback(answer, EXECUTIONS[name].workflow);
      const recorded = recordedRollback(asRecorded(answer));
      const ids = [
        ...new Set(
          simulated.each.flatMap((one) => [
            one.id,
            ...one.participants.map((p) => p.stepId),
          ]),
        ),
      ];
      expect(words(recorded, ids), name).toEqual(words(simulated, ids));
      expect(tones(recorded, ids), name).toEqual(tones(simulated, ids));
    }
  });
});
