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
// Debug in editor on a run whose transaction was undone or left in doubt:
// the failure that undid it becomes a simulated failure with what it may
// have changed, never its message, marked as run data; an undo step's result
// the platform withholds and an undo that got stuck are left out, each
// saying why; what Studio keeps is test data it can save.
import { execFileSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import {
  leftOutText,
  testDataFromRun,
  UNDO_LEFT_OUT,
  type RunSource,
} from "../src/app/editor/runs-tab/debug-from-run";
import { DebugRun, type DebugHost } from "../src/app/editor/runs-tab/debug-run";
import type { RunData } from "../src/app/editor/runs-tab/run-data";
import type { RunRecord } from "../src/app/editor/runs-tab/run-record";
import type { ChosenRun } from "../src/app/editor/runs-tab/runs-tab-state";
import type { RunTransactionView } from "../src/app/editor/runs-tab/run-transactions";
import type { StepFingerprints } from "../src/app/editor/state/fingerprints";
import {
  emptyTestData,
  fitsTestData,
} from "../src/app/editor/state/test-data-ops";
import type { TestDataService } from "../src/app/editor/state/test-data-service";
import type { StudioTestData } from "../src/app/editor/state/test-data";
import { readTestData } from "../src/app/editor/state/test-data-read";
import { StructuredCanvasAdapter, type Workflow } from "../src/app/model";
import type { StepFact } from "../src/app/operate/run-contracts";
import { python, pythonAvailable, repository } from "./python-path";

/** The platform's model of test data, and whether each document counts as run data. */
const KEPT = `
import json, sys
from firefly_weave.contracts.studio_documents import StudioTestData
from firefly_weave.definitions.studio_documents import run_data
documents = [StudioTestData.model_validate_json(json.dumps(item)) for item in json.loads(sys.stdin.buffer.read().decode("utf-8"))]
print("ok", *[str(run_data(document)[0]).lower() for document in documents])
`;
const RUN = "1c0d7f8a-4d66-4e7c-8b62-5a2c1d3e8f12";
const ENV = "4a1b2c3d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
const NOW = "2026-11-25T12:00:00Z";
const undo = { kind: "action", uses: "undo.run@1.0.0", with: {} };
const action = (id: string, extra: object = {}) => ({
  id,
  kind: "action",
  uses: `${id}.run@1.0.0`,
  with: {},
  ...extra,
});
const move = (onError?: string): Workflow =>
  ({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "move-between-systems", version: "1.4.0" },
    spec: {
      steps: [
        {
          id: "move",
          kind: "transaction",
          ...(onError ? { onError } : {}),
          steps: [
            action("hold", { confirm: undo, cancel: undo }),
            action("add", { compensate: undo }),
            { id: "check", kind: "transform", value: { literal: 1 } },
            action("submit", { irreversible: true }),
          ],
          output: { literal: null },
        },
      ],
    },
  }) as unknown as Workflow;
const prints = new Map<string, StepFingerprints>(
  ["hold", "add", "check", "submit", "move"].map((id, i) => [
    id,
    {
      config: `fnv1a64:${String(10 + i).padStart(16, "0")}`,
      contract: `fnv1a64:${String(20 + i).padStart(16, "0")}`,
      upstream: `fnv1a64:${String(30 + i).padStart(16, "0")}`,
      execution: `fnv1a64:${String(40 + i).padStart(16, "0")}`,
    } as StepFingerprints,
  ]),
);
function fact(
  key: string,
  status: StepFact["status"],
  extra: Partial<StepFact> = {},
): StepFact {
  return {
    node_id: key.replace(/[[#].*$/, ""),
    instance_key: key,
    iteration: [],
    kind: "action",
    status,
    scheduled_at: null,
    started_at: null,
    ended_at: null,
    duration_ms: null,
    queue_wait_ms: null,
    attempts: 1,
    worker_id: "w-7",
    error_code: null,
    child_run_id: null,
    log_entries: 0,
    ...extra,
  };
}
const record = (status: RunRecord["status"]): RunRecord => ({
  id: RUN,
  versionId: "9e8f5b0c-cfee-4af4-8dea-d2ae9fb06b9a",
  status,
  input: { moveId: "m-1", quantity: 2 },
  completed: {
    hold: { output: { holdId: "h-1" } },
    add: { output: { id: "a-1" } },
  },
  active: [],
  waiting: [],
  incidents: [],
});
const participant = (
  node: string,
  mode: "reserve" | "compensate" | "irreversible",
  status: string,
  effect: "none" | "unknown" = "unknown",
  attempts?: number,
) => ({
  node_id: node,
  instance_key: node,
  mode,
  status,
  effect,
  ...(attempts === undefined ? {} : { attempts }),
});
const view = (
  state: string,
  participants: object[],
  cause: object | null,
): RunTransactionView =>
  ({
    instance_key: "move",
    node_id: "move",
    iteration: [],
    state,
    ...(cause ? { cause } : {}),
    opened_at: NOW,
    participants,
  }) as unknown as RunTransactionView;
const REFUSED = {
  code: "REQUEST_REFUSED",
  instance_key: "submit",
  effect: "none",
};
const FACTS = [
  fact("hold", "succeeded", { output: { holdId: "h-1" } }),
  fact("add", "succeeded", { output: { id: "a-1" } }),
  fact("submit", "failed", {
    error_code: "REQUEST_REFUSED",
    attempts: 2,
  }),
];
function source(
  status: RunRecord["status"],
  views: RunTransactionView[] | undefined,
  facts: StepFact[] = FACTS,
): RunSource {
  return {
    runId: RUN,
    environmentId: ENV,
    status,
    record: record(status),
    definition: move(),
    failedStep:
      status === "failed" ? { key: "submit", code: "REQUEST_REFUSED" } : null,
    facts,
    ...(views ? { transactions: views } : {}),
  };
}
const built = (from: RunSource, onto: Workflow = move()) => {
  const result = testDataFromRun(from, onto, prints, NOW);
  if ("refused" in result) throw Error(result.refused);
  return result;
};
const UNDONE = view(
  "compensated",
  [
    participant("hold", "reserve", "cancelled"),
    participant("add", "compensate", "undone"),
    participant("submit", "irreversible", "failed", "none"),
  ],
  REFUSED,
);

describe("Debug in editor on a run whose transaction was undone", () => {
  it("copies the failure that undid it with what it may have changed, as run data", () => {
    const { data, pointers } = built(source("failed", [UNDONE]));
    expect(data.errors).toEqual({
      submit: {
        code: "REQUEST_REFUSED",
        attempts: 2,
        effect: "none",
        source: "run",
        runId: RUN,
        runEnvironmentId: ENV,
      },
    });
    expect(pointers).toContain("/errors/submit");
    // What the steps before it returned stays pinned, so Execute undoes them.
    expect(Object.keys(data.pins ?? {}).sort()).toEqual(["add", "hold"]);
    expect(fitsTestData(data)).toBe(true);
    // Studio reads it back as the test data it saved.
    expect(readTestData(JSON.parse(JSON.stringify(data)))).toEqual({
      ok: true,
      data,
    });
  });

  it("never copies a message, and keeps an unknown outcome with what it may have changed", () => {
    const unknown = view(
      "in_doubt",
      [
        participant("hold", "reserve", "done"),
        participant("add", "compensate", "done"),
        participant("submit", "irreversible", "unknown", "unknown"),
      ],
      { code: "TIMEOUT", instance_key: "submit", effect: "unknown" },
    );
    const { data } = built(
      source(
        "suspended",
        [unknown],
        [
          ...FACTS.slice(0, 2),
          fact("submit", "failed", { error_code: "TIMEOUT", attempts: 1 }),
        ],
      ),
    );
    expect(data.errors?.["submit"]).toEqual({
      code: "TIMEOUT",
      outcome: "unknown",
      attempts: 1,
      effect: "unknown",
      source: "run",
      runId: RUN,
      runEnvironmentId: ENV,
    });
    expect(JSON.stringify(data)).not.toMatch(/message/i);
  });

  it("copies a step with nothing to undo whose failure undid the transaction, without an effect", () => {
    const view_ = view(
      "compensated",
      [
        participant("hold", "reserve", "cancelled"),
        participant("add", "compensate", "undone"),
      ],
      { code: "NOT_ALLOWED", instance_key: "check", effect: "unknown" },
    );
    const { data } = built(
      source(
        "failed",
        [view_],
        [
          ...FACTS.slice(0, 2),
          fact("check", "failed", {
            kind: "transform",
            error_code: "NOT_ALLOWED",
          }),
        ],
      ),
    );
    expect(data.errors?.["check"]).toEqual({
      code: "NOT_ALLOWED",
      attempts: 1,
      source: "run",
      runId: RUN,
      runEnvironmentId: ENV,
    });
  });

  it("leaves out an undo that got stuck, saying how to simulate it", () => {
    const stuck = view(
      "in_doubt",
      [
        participant("hold", "reserve", "done"),
        participant("add", "compensate", "undo_stuck", "unknown", 10),
        participant("submit", "irreversible", "failed", "none"),
      ],
      REFUSED,
    );
    const { data, leftOut } = built(source("suspended", [stuck]));
    expect(data.errors?.["submit"]).toMatchObject({ code: "REQUEST_REFUSED" });
    expect(Object.keys(data.errors ?? {})).toEqual(["submit"]);
    expect(leftOut).toContainEqual({
      key: "add#undo",
      reason: "undo",
      detail: UNDO_LEFT_OUT,
    });
    expect(UNDO_LEFT_OUT).toBe(
      "Studio can't copy an undo's failure from a run yet. Use Fail its undo.",
    );
    expect(leftOutText(leftOut, [])).toBe(
      "Left out 1 value: add#undo (use Fail its undo).",
    );
  });

  it("says the run didn't record an undone or released step's undo when no fact holds it", () => {
    const { leftOut } = built(source("failed", [UNDONE]));
    const missing = "the platform didn't record it";
    expect(leftOut).toEqual([
      { key: "hold#cancel", reason: "not-recorded", detail: missing },
      { key: "add#undo", reason: "not-recorded", detail: missing },
    ]);
    expect(leftOutText(leftOut, [])).toBe(
      "Left out 2 values: hold#cancel (the platform didn't record it), add#undo (the platform didn't record it).",
    );
    // A step that failed has no undo to record.
    expect(leftOut.map(({ key }) => key)).not.toContain("submit#undo");
  });

  it("leaves out an undo step's result the platform withholds, and pins one it recorded", () => {
    const withheld = fact("add#undo", "succeeded", {
      omissions: [{ path: "/output", reason: "classification_unavailable" }],
    });
    const { data, leftOut } = built(
      source("failed", [UNDONE], [...FACTS, withheld]),
    );
    expect(data.pins?.["add#undo"]).toBeUndefined();
    expect(leftOut.map(({ key, reason }) => [key, reason])).toEqual([
      ["add#undo", "omitted"],
      ["hold#cancel", "not-recorded"],
    ]);
    const recorded = fact("hold#cancel", "succeeded", {
      output: { released: true },
    });
    const pinned = built(source("failed", [UNDONE], [...FACTS, recorded]));
    expect(pinned.data.pins?.["hold#cancel"]).toMatchObject({
      output: { released: true },
      source: "run",
      runId: RUN,
      runEnvironmentId: ENV,
    });
  });

  it("copies no failure of the transaction itself: its steps' reproduce it", () => {
    const continued: RunSource = {
      ...source("succeeded", [UNDONE]),
      definition: move("continue"),
      record: {
        ...record("succeeded"),
        completed: {
          ...record("succeeded").completed,
          move: { error: { code: "WV-TXN-ROLLED-BACK", message: "Undone." } },
        },
      },
    };
    const { data } = built(continued, move("continue"));
    expect(Object.keys(data.errors ?? {})).toEqual(["submit"]);
  });

  it.skipIf(!pythonAvailable())(
    "is test data the platform keeps, its run data marked",
    () => {
      const stuck = view(
        "in_doubt",
        [
          participant("hold", "reserve", "done"),
          participant("add", "compensate", "undo_stuck", "unknown", 10),
          participant("submit", "irreversible", "unknown", "unknown"),
        ],
        { code: "TIMEOUT", instance_key: "submit", effect: "unknown" },
      );
      const documents = [
        built(source("failed", [UNDONE])).data,
        built(
          source(
            "suspended",
            [stuck],
            [
              ...FACTS.slice(0, 2),
              fact("submit", "failed", { error_code: "TIMEOUT" }),
            ],
          ),
        ).data,
      ];
      // Each holds the failure that undid its transaction, with its effect.
      expect(documents.map((document) => document.errors?.["submit"])).toEqual([
        expect.objectContaining({ code: "REQUEST_REFUSED", effect: "none" }),
        expect.objectContaining({
          code: "TIMEOUT",
          outcome: "unknown",
          effect: "unknown",
        }),
      ]);
      expect(
        execFileSync(python, ["-c", KEPT], {
          cwd: repository,
          input: JSON.stringify(documents),
          encoding: "utf8",
          timeout: 180_000,
        }).trim(),
      ).toBe("ok true true");
    },
  );

  it("copies what it always did when the run's transactions weren't read", () => {
    const { data } = built(source("failed", undefined));
    expect(data.errors).toBeUndefined();
  });
});

describe("Debug in editor from the Runs tab", () => {
  /** Studio's shell around Debug in editor; the test data service keeps what it is given. */
  function debugHost() {
    const kept: StudioTestData[] = [];
    const model = new StructuredCanvasAdapter();
    model.definition = move();
    const testData = {
      place: () => ({ kind: "connected", draftId: "d-1" }),
      data: () => emptyTestData(NOW),
      keepFromRun: async (next: StudioTestData) => {
        kept.push(next);
        return { ok: true, previous: next, version: 1, refused: [] };
      },
    } as unknown as TestDataService;
    const host: DebugHost = {
      profile: { environmentId: ENV },
      api: {
        environment: ENV,
        request: async <T>(path: string) => {
          expect(path).toContain(`/runs/${RUN}/steps`);
          return { items: FACTS, next_cursor: null } as T;
        },
      },
      draftId: "d-1",
      model,
      stepDataScope: "account-a|env-1",
      testData,
      dialogs: { choose: async () => null },
      runNotice: null,
      can: () => true,
      notify: () => undefined,
      selectTab: async () => true,
      executeIn: async () => ({ kind: "stale" }),
      openStepDetails: async () => true,
    };
    return { host, kept };
  }
  const chosen = (transactions: ChosenRun["transactions"]): ChosenRun => ({
    id: RUN,
    summary: null,
    record: record("failed"),
    definition: move(),
    overlay: null,
    // Debug in editor only asks whether the run's data was read.
    data: {} as RunData,
    following: false,
    stopped: false,
    limited: false,
    error: null,
    transactions,
  });

  it("copies the failure that undid the run's transaction, with its effect, from what the tab read", async () => {
    const { host, kept } = debugHost();
    const outcome = await new DebugRun().run(
      host,
      chosen({ views: [UNDONE], more: false }),
      "debug",
    );
    expect(outcome).toEqual({ kind: "done", stoppedAt: null });
    expect(kept).toHaveLength(1);
    expect(kept[0].errors).toEqual({
      submit: {
        code: "REQUEST_REFUSED",
        attempts: 2,
        effect: "none",
        source: "run",
        runId: RUN,
        runEnvironmentId: ENV,
      },
    });
  });
});
