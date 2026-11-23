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
// Activating a version sets up the workflow's platform event trigger: create
// it, move it to the new version, or replace it when the version reacts to
// other events or workflows (create the new one, then stop the old one). It
// never touches another workflow's trigger, never leaves two triggers
// reacting unseen, never skips waiting starts without asking, and stops
// nothing someone changed since the dialog read it. The fake platform below
// answers as the platform's event trigger routes do.
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import type { PlanActivation } from "../src/app/editor/activation/trigger-plan";
import type { TriggerApi } from "../src/app/editor/activation/trigger-resources-api";
import {
  applyEventRow,
  bindingName,
  eventBindingPlan,
  eventRowActionable,
  sameTrigger,
  type EventRow,
  type EventTriggerView,
  type PendingStop,
} from "../src/app/editor/activation/event-bindings";
import type { EventDeclaration } from "../src/app/editor/triggers/event-trigger";

const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";
const MESSAGES: Record<string, string> = {
  "WV-TRIGGER-CONFLICT":
    "Another enabled platform-event trigger has this name.",
  "WV-TRIGGER-LIMIT":
    "This environment already has 64 platform-event triggers. Disable one first.",
};
const refusal = (status: number, code: string, message = MESSAGES[code]!) =>
  new ApiError(status, { code, message });
const FORBIDDEN = () => refusal(403, "WV-FORBIDDEN", "Access denied");

type Wire = Record<string, unknown>;
interface Sent {
  key: string;
  revision?: number;
  body?: unknown;
}
interface Seed {
  id: string;
  name: string;
  activation: string;
  events?: string[];
  sources?: string[];
  mayReconcile?: boolean | null;
  revision?: number;
  disabled?: boolean;
}

/** Which workflow each activation runs, as the platform knows it. */
const WORKFLOW_OF = new Map([
  ["a-older", "notify-team"],
  ["a-old", "notify-team"],
  ["a-new", "notify-team"],
  ["a-other", "sync-references"],
]);

/** The platform's event trigger routes for one environment, with faults to inject. */
class FakePlatform implements TriggerApi {
  /** The environment the person has chosen in Studio; the platform serves only ENV. */
  environment = ENV;
  readonly sent: Sent[] = [];
  readonly triggers: Wire[] = [];
  readonly starts = new Map<string, Wire[]>();
  readonly capabilities = new Set(["trigger.manage", "run.start", "run.read"]);
  /** The next request with this key is applied, then its answer is lost. */
  readonly lose = new Set<string>();
  /** The next request with this key is refused before anything happens. */
  readonly refuse = new Map<string, Error>();
  /** Runs once, just before the platform handles the next request with this key. */
  readonly before = new Map<string, () => void>();
  private created = 0;

  seed(seed: Seed) {
    this.triggers.push(
      view({
        events: ["transaction.in_doubt"],
        sources: ["move-between-systems"],
        mayReconcile: false,
        revision: 1,
        disabled: false,
        ...seed,
      }),
    );
  }
  views(): EventTriggerView[] {
    return structuredClone(this.triggers) as unknown as EventTriggerView[];
  }
  find(id: string) {
    return this.triggers.find((trigger) => trigger["id"] === id);
  }
  enabled() {
    return this.triggers.filter((trigger) => !trigger["disabled"]);
  }
  writes() {
    return this.sent
      .map((call) => call.key)
      .filter((key) => key.startsWith("POST"));
  }

  async page(): Promise<never> {
    throw Error("Event triggers are read with explicit paths.");
  }

  /** `event_triggers.list`, `.read` and `.starts`. */
  async request<T>(path: string, method = "GET"): Promise<T> {
    if (!path.startsWith(`${ENV}/`)) throw notFound();
    const [local = "", query = ""] = path.slice(ENV.length).split("?");
    this.handle(`${method} ${local}`);
    const params = new URLSearchParams(query);
    if (method !== "GET") throw notFound();
    if (local === "/event-triggers")
      return paged(
        [...this.triggers].sort((a, b) =>
          String(a["id"]).localeCompare(String(b["id"])),
        ),
        params,
      ) as T;
    const [, , id = "", starts] = local.split("/");
    const trigger = this.find(decodeURIComponent(id));
    if (!trigger) throw notFound();
    if (starts !== "starts") return structuredClone(trigger) as T;
    if (!this.capabilities.has("run.read")) throw FORBIDDEN();
    return paged(this.starts.get(decodeURIComponent(id)) ?? [], params) as T;
  }

  async mutate<T>(
    path: string,
    method: string,
    body: unknown,
    revision?: number,
  ): Promise<T> {
    if (!path.startsWith(`${ENV}/`)) throw notFound();
    const local = path.slice(ENV.length);
    const key = `${method} ${local}`;
    this.sent.push({ key, revision, body: structuredClone(body) });
    this.handle(key, false);
    const answer = this.write(local, body as Wire, revision);
    if (this.lose.delete(key)) throw new TypeError("Failed to fetch");
    return structuredClone(answer) as T;
  }

  private handle(key: string, log = true) {
    if (log) this.sent.push({ key });
    const change = this.before.get(key);
    if (change) {
      this.before.delete(key);
      change();
    }
    const refused = this.refuse.get(key);
    if (refused) {
      this.refuse.delete(key);
      throw refused;
    }
  }

  private require(...capabilities: string[]) {
    if (capabilities.some((capability) => !this.capabilities.has(capability)))
      throw FORBIDDEN();
  }

  private write(local: string, body: Wire, revision?: number): Wire {
    const [, , id, action] = local.split("/");
    if (!id) {
      this.require("trigger.manage", "run.start", "run.read");
      if (body["may_reconcile"]) this.require("transaction.reconcile");
      if (!WORKFLOW_OF.has(String(body["activation_id"]))) throw notFound();
      if (this.enabled().some((trigger) => trigger["name"] === body["name"]))
        throw refusal(409, "WV-TRIGGER-CONFLICT");
      if (this.enabled().length >= 64) throw refusal(409, "WV-TRIGGER-LIMIT");
      const created = view({
        id: `t-made-${++this.created}`,
        name: String(body["name"]),
        activation: String(body["activation_id"]),
        events: body["events"] as string[],
        ...(body["source_workflows"]
          ? { sources: body["source_workflows"] as string[] }
          : {}),
        mayReconcile: body["may_reconcile"] === true,
        revision: 1,
        disabled: false,
        principal: "p-me",
      });
      this.triggers.push(created);
      return created;
    }
    const trigger = this.find(decodeURIComponent(id));
    if (!trigger) throw notFound();
    if (action === "disable") {
      this.require("trigger.manage");
      if (!trigger["disabled"]) {
        if (revision !== undefined && revision !== trigger["revision"])
          throw refusal(
            409,
            "WV-REVISION",
            "The trigger changed; read it again before stopping it",
          );
        trigger["disabled"] = true;
        trigger["revision"] = Number(trigger["revision"]) + 1;
      }
      return trigger;
    }
    this.require("trigger.manage", "run.start", "run.read");
    if (trigger["may_reconcile"]) this.require("transaction.reconcile");
    if (revision === undefined)
      throw refusal(422, "WV-ETAG", "If-Match requires a positive revision");
    if (revision !== trigger["revision"])
      throw refusal(
        409,
        "WV-REVISION",
        "The trigger changed; read it again before moving it",
      );
    if (trigger["disabled"])
      throw refusal(
        409,
        "WV-TRIGGER-DISABLED",
        "Create a new trigger; this one is stopped.",
      );
    const workflow = WORKFLOW_OF.get(String(body["activation_id"]));
    if (!workflow) throw notFound();
    trigger["activation_id"] = body["activation_id"];
    trigger["workflow_name"] = workflow;
    trigger["principal_id"] = "p-me";
    trigger["revision"] = Number(trigger["revision"]) + 1;
    return trigger;
  }
}

const notFound = () => refusal(404, "WV-NOT-FOUND", "Event trigger not found");

/** One page of `items` in the order given, as the platform pages a list: `limit`, then `cursor` after an item's ID. */
function paged(items: readonly Wire[], params: URLSearchParams) {
  const limit = Number(params.get("limit") ?? "50");
  const cursor = params.get("cursor");
  const start = cursor
    ? items.findIndex((item) => item["id"] === cursor) + 1
    : 0;
  const page = items.slice(start, start + limit);
  return {
    items: structuredClone(page),
    next_cursor:
      start + limit < items.length
        ? String(page[page.length - 1]!["id"])
        : null,
  };
}

/**
 * A trigger as the platform answers it: `source_workflows` left out for any
 * workflow, and `workflow_name` the workflow its activation runs.
 */
function view(seed: Seed & { principal?: string }): Wire {
  const workflow = WORKFLOW_OF.get(seed.activation);
  if (!workflow)
    throw Error(`The platform knows no activation ${seed.activation}`);
  return {
    name: seed.name,
    activation_id: seed.activation,
    events: [...(seed.events ?? [])],
    ...(seed.sources ? { source_workflows: [...seed.sources] } : {}),
    ...(seed.mayReconcile === null || seed.mayReconcile === undefined
      ? {}
      : { may_reconcile: seed.mayReconcile }),
    id: seed.id,
    workflow_name: workflow,
    principal_id: seed.principal ?? "p-owner",
    revision: seed.revision ?? 1,
    disabled: seed.disabled ?? false,
    created_at: "2026-11-20T09:00:00Z",
  };
}

const requested = (id: string): Wire => ({
  id,
  trigger_id: "t-1",
  event_id: "e-1",
  event_type: "transaction.in_doubt",
  source_run_id: "r-1",
  root_event_id: "e-1",
  depth: 1,
  status: "requested",
  requested_at: "2026-11-20T09:30:00Z",
});
/** `count` starts that each started a run, newest first. */
const startedRuns = (count: number): Wire[] =>
  Array.from({ length: count }, (_, index) => ({
    ...requested(`s-${index}`),
    status: "started",
    run_id: `r-${index}`,
  }));

const ACTIVATIONS: PlanActivation[] = [
  { id: "a-older", workflow: "notify-team", version: "1.0.0" },
  { id: "a-old", workflow: "notify-team", version: "1.1.0" },
  { id: "a-other", workflow: "sync-references", version: "2.0.0" },
];
const ATTENTION: EventDeclaration = {
  events: ["transaction.in_doubt"],
  from: ["move-between-systems"],
};
const WIDER: EventDeclaration = {
  events: ["transaction.compensated", "transaction.in_doubt"],
  from: null,
};

function planFor(
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
const apply = (
  platform: FakePlatform,
  row: EventRow,
  options: { stopWithWaiting?: boolean } = {},
) =>
  applyEventRow(platform, row, "a-new", { ...options, environment: "local" });
const only = (rows: EventRow[]) => {
  expect(rows).toHaveLength(1);
  return rows[0]!;
};

describe("naming a new trigger", () => {
  it("uses the workflow's name, then the first free number", () => {
    expect(bindingName("notify-team", new Set())).toBe("notify-team");
    expect(bindingName("notify-team", new Set(["notify-team"]))).toBe(
      "notify-team-2",
    );
    expect(
      bindingName(
        "notify-team",
        new Set(["notify-team", "notify-team-2", "notify-team-4"]),
      ),
    ).toBe("notify-team-3");
  });
});

describe("creating the trigger", () => {
  it("creates one named after the workflow for every declared event and source", async () => {
    const platform = new FakePlatform();
    const row = only(planFor(platform));
    expect(row).toMatchObject({
      kind: "create",
      name: "notify-team",
      request: {
        name: "notify-team",
        events: ["transaction.in_doubt"],
        source_workflows: ["move-between-systems"],
        may_reconcile: false,
      },
      text: "Create platform event trigger notify-team for Needs attention · from move-between-systems",
    });
    const result = await apply(platform, row);
    expect(result).toMatchObject({
      status: "created",
      id: "t-made-1",
      text: "Created notify-team",
    });
    expect(platform.enabled()).toMatchObject([
      {
        id: "t-made-1",
        name: "notify-team",
        activation_id: "a-new",
        events: ["transaction.in_doubt"],
        source_workflows: ["move-between-systems"],
        may_reconcile: false,
      },
    ]);
  });

  it("leaves the sources out for any workflow", async () => {
    const platform = new FakePlatform();
    const row = only(planFor(platform, { declaration: WIDER }));
    await apply(platform, row);
    expect(
      platform.sent.find((call) => call.key === "POST /event-triggers"),
    ).toMatchObject({
      body: {
        name: "notify-team",
        activation_id: "a-new",
        events: ["transaction.compensated", "transaction.in_doubt"],
        may_reconcile: false,
      },
    });
    expect(
      platform.sent.find((call) => call.key === "POST /event-triggers")!.body,
    ).not.toHaveProperty("source_workflows");
  });

  it("takes the next free name when another trigger has the workflow's name", () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-9", name: "notify-team", activation: "a-other" });
    platform.seed({ id: "t-8", name: "notify-team-2", activation: "a-other" });
    expect(only(planFor(platform))).toMatchObject({
      kind: "create",
      name: "notify-team-3",
    });
  });

  it("says which permissions a refused create needs", async () => {
    const platform = new FakePlatform();
    platform.capabilities.delete("run.read");
    const result = await apply(platform, only(planFor(platform)));
    expect(result).toMatchObject({
      status: "failed",
      retry: "create",
      text: "Couldn't create notify-team: you need to manage triggers, start runs and read runs in local.",
    });
  });

  it("fails safe when the environment has too many triggers to check", async () => {
    const platform = new FakePlatform();
    for (let index = 0; index < 501; index++)
      platform.seed({
        id: `t-${String(index).padStart(3, "0")}`,
        name: `old-${index}`,
        activation: "a-other",
        disabled: true,
      });
    const row: EventRow = {
      kind: "create",
      workflow: "notify-team",
      name: "notify-team",
      request: {
        name: "notify-team",
        events: ["transaction.in_doubt"],
        may_reconcile: false,
      },
      text: "",
    };
    expect(await apply(platform, row)).toMatchObject({
      status: "failed",
      text: "Couldn't create notify-team: Too many triggers to check here. Use the CLI.",
    });
    expect(platform.writes()).toEqual([]);
  });
});

describe("a create whose answer may have been lost", () => {
  it("adopts a create whose answer was lost instead of creating a second trigger", async () => {
    const platform = new FakePlatform();
    platform.lose.add("POST /event-triggers");
    const row = only(planFor(platform));
    const lost = await apply(platform, row);
    expect(lost).toMatchObject({ status: "failed", retry: "create" });
    const retried = await apply(platform, row);
    expect(retried).toMatchObject({
      status: "created",
      id: "t-made-1",
      text: "Created notify-team",
    });
    expect(platform.enabled()).toHaveLength(1);
    expect(platform.writes()).toEqual(["POST /event-triggers"]);
  });

  it("adopts an equal trigger on a conflict", async () => {
    const platform = new FakePlatform();
    const row = only(planFor(platform));
    // An earlier attempt for this activation lands just before this one.
    platform.before.set("POST /event-triggers", () =>
      platform.seed({
        id: "t-earlier",
        name: "notify-team",
        activation: "a-new",
      }),
    );
    expect(await apply(platform, row)).toMatchObject({
      status: "created",
      id: "t-earlier",
    });
    expect(platform.enabled()).toHaveLength(1);
    expect(platform.writes()).toEqual(["POST /event-triggers"]);
  });

  it("takes the next suffix when the name belongs to a different trigger", async () => {
    const platform = new FakePlatform();
    const row = only(planFor(platform));
    platform.before.set("POST /event-triggers", () =>
      platform.seed({
        id: "t-someone",
        name: "notify-team",
        activation: "a-new",
        events: ["transaction.completed"],
      }),
    );
    expect(await apply(platform, row)).toMatchObject({
      status: "created",
      text: "Created notify-team-2",
    });
    expect(platform.enabled().map((trigger) => trigger["name"])).toEqual([
      "notify-team",
      "notify-team-2",
    ]);
  });

  it("creates at most once more after a conflict, then fails with the platform's sentence", async () => {
    const platform = new FakePlatform();
    const row = only(planFor(platform));
    let taken = 0;
    const takeNext = () =>
      platform.seed({
        id: `t-race-${++taken}`,
        name: taken === 1 ? "notify-team" : `notify-team-${taken}`,
        activation: "a-other",
      });
    platform.before.set("POST /event-triggers", () => {
      takeNext();
      platform.before.set("POST /event-triggers", takeNext);
    });
    expect(await apply(platform, row)).toMatchObject({
      status: "failed",
      retry: "create",
      text: "Couldn't create notify-team-2: Another enabled platform-event trigger has this name.",
    });
    expect(platform.writes()).toEqual([
      "POST /event-triggers",
      "POST /event-triggers",
    ]);
  });

  it("adopts a create whose name took a suffix before its answer was lost", async () => {
    const platform = new FakePlatform();
    const row = only(planFor(platform));
    platform.seed({
      id: "t-someone",
      name: "notify-team",
      activation: "a-other",
    });
    platform.lose.add("POST /event-triggers");
    expect(await apply(platform, row)).toMatchObject({ status: "failed" });
    expect(await apply(platform, row)).toMatchObject({
      status: "created",
      text: "Created notify-team-2",
    });
    expect(platform.writes()).toEqual(["POST /event-triggers"]);
  });

  it("never adopts a trigger with a name Studio doesn't give this workflow", async () => {
    const platform = new FakePlatform();
    const row = only(planFor(platform));
    platform.seed({ id: "t-ops", name: "ops-alerts", activation: "a-new" });
    expect(await apply(platform, row)).toMatchObject({
      status: "created",
      id: "t-made-1",
      text: "Created notify-team",
    });
  });

  it("only adopts a trigger equal in every respect", () => {
    const body = {
      name: "notify-team",
      activation_id: "a-new",
      events: ["transaction.in_doubt" as const],
      source_workflows: ["move-between-systems"],
      may_reconcile: false,
    };
    const equal = view({
      id: "t-1",
      name: "notify-team",
      activation: "a-new",
      events: ["transaction.in_doubt"],
      sources: ["move-between-systems"],
      mayReconcile: false,
    }) as unknown as EventTriggerView;
    expect(sameTrigger(equal, body, "notify-team")).toBe(true);
    const differences: Partial<EventTriggerView>[] = [
      { name: "notify-team-2" },
      { activation_id: "a-old" },
      { workflow_name: "sync-references" },
      { events: ["transaction.completed"] },
      { source_workflows: ["sync-references"] },
      { may_reconcile: true },
      { disabled: true },
    ];
    for (const different of differences)
      expect(sameTrigger({ ...equal, ...different }, body, "notify-team")).toBe(
        false,
      );
    const anyWorkflow = { ...equal } as Partial<EventTriggerView>;
    delete anyWorkflow.source_workflows;
    expect(
      sameTrigger(anyWorkflow as EventTriggerView, body, "notify-team"),
    ).toBe(false);
  });
});

describe("moving the trigger", () => {
  it("moves the stored trigger with the revision it read when the events and sources are unchanged", async () => {
    const platform = new FakePlatform();
    platform.seed({
      id: "t-1",
      name: "notify-team",
      activation: "a-old",
      revision: 3,
    });
    const row = only(planFor(platform, { storedId: "t-1" }));
    expect(row).toMatchObject({
      kind: "retarget",
      from: "1.1.0",
      to: "1.2.0",
      reconciles: false,
      byName: false,
      text: "Move platform event trigger notify-team from 1.1.0 to 1.2.0. Runs start as you after the move, including events waiting to start.",
    });
    expect(await apply(platform, row)).toMatchObject({
      status: "moved",
      id: "t-1",
      text: "Moved notify-team to 1.2.0",
    });
    expect(platform.sent.at(-1)).toMatchObject({
      key: "POST /event-triggers/t-1/retarget",
      revision: 3,
      body: { activation_id: "a-new" },
    });
    expect(platform.find("t-1")).toMatchObject({
      activation_id: "a-new",
      revision: 4,
    });
  });

  it("moves a stored trigger that reacts to any workflow", async () => {
    // The platform leaves source_workflows out; the declaration says from: null.
    const platform = new FakePlatform();
    platform.seed({
      id: "t-1",
      name: "notify-team",
      activation: "a-old",
      events: ["transaction.in_doubt", "transaction.compensated"],
      sources: undefined,
    });
    const row = only(
      planFor(platform, { storedId: "t-1", declaration: WIDER }),
    );
    expect(row).toMatchObject({ kind: "retarget", binding: { id: "t-1" } });
    expect(await apply(platform, row)).toMatchObject({ status: "moved" });
    expect(platform.writes()).toEqual(["POST /event-triggers/t-1/retarget"]);
  });

  it("says a moved trigger may resolve transactions as the mover", () => {
    const platform = new FakePlatform();
    platform.seed({
      id: "t-1",
      name: "notify-team",
      activation: "a-old",
      mayReconcile: true,
    });
    expect(only(planFor(platform, { storedId: "t-1" }))).toMatchObject({
      kind: "retarget",
      reconciles: true,
      text: "Move platform event trigger notify-team from 1.1.0 to 1.2.0. Runs start as you after the move, including events waiting to start. Its runs may resolve transactions, as you after the move. This needs permission to reconcile transactions.",
    });
  });

  it("says when someone moved it first", async () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    const row = only(planFor(platform, { storedId: "t-1" }));
    platform.find("t-1")!["revision"] = 2;
    platform.find("t-1")!["activation_id"] = "a-older";
    expect(await apply(platform, row)).toMatchObject({
      status: "changed",
      text: "notify-team changed since you opened this. Review it.",
    });
  });

  it("reads a lost move as moved when it is tried again", async () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    const row = only(planFor(platform, { storedId: "t-1" }));
    platform.lose.add("POST /event-triggers/t-1/retarget");
    expect(await apply(platform, row)).toMatchObject({ status: "failed" });
    expect(await apply(platform, row)).toMatchObject({
      status: "moved",
      id: "t-1",
    });
  });

  it("never reads a lost move as moved once someone stopped the trigger", async () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    const row = only(planFor(platform, { storedId: "t-1" }));
    platform.lose.add("POST /event-triggers/t-1/retarget");
    expect(await apply(platform, row)).toMatchObject({ status: "failed" });
    // The move landed on the new activation; then someone stopped the trigger.
    Object.assign(platform.find("t-1")!, { disabled: true, revision: 3 });
    expect(await apply(platform, row)).toMatchObject({
      status: "changed",
      text: "notify-team changed since you opened this. Review it.",
    });
  });
});

describe("replacing the trigger when the version reacts to other events", () => {
  it("creates the new trigger first, then stops the old one with the revision it read", async () => {
    const platform = new FakePlatform();
    platform.seed({
      id: "t-1",
      name: "notify-team",
      activation: "a-old",
      revision: 2,
    });
    const row = only(
      planFor(platform, { storedId: "t-1", declaration: WIDER }),
    );
    expect(row).toMatchObject({
      kind: "replace",
      name: "notify-team-2",
      request: {
        name: "notify-team-2",
        events: ["transaction.compensated", "transaction.in_doubt"],
        may_reconcile: false,
      },
      from: "1.1.0",
      to: "1.2.0",
      reconciles: false,
      before: ATTENTION,
      after: WIDER,
      text: "Replace platform event trigger notify-team: 1.2.0 reacts to Needs attention and Undone from any workflow in this project (now: Needs attention from move-between-systems). Studio creates notify-team-2, then stops notify-team.",
    });
    expect(await apply(platform, row)).toMatchObject({
      status: "replaced",
      id: "t-made-1",
      text: "Replaced notify-team by notify-team-2",
      pendingStop: null,
    });
    expect(platform.writes()).toEqual([
      "POST /event-triggers",
      "POST /event-triggers/t-1/disable",
    ]);
    expect(
      platform.sent
        .filter((call) => call.key === "POST /event-triggers/t-1/disable")
        .map((call) => call.revision),
    ).toEqual([2]);
    expect(platform.enabled().map((trigger) => trigger["id"])).toEqual([
      "t-made-1",
    ]);
  });

  it("says so when the version names no workflow to react to", () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    const row = only(
      planFor(platform, {
        storedId: "t-1",
        declaration: { events: ["transaction.in_doubt"], from: [] },
      }),
    );
    expect(row.text).toBe(
      "Replace platform event trigger notify-team: 1.2.0 reacts to Needs attention from no workflow (now: Needs attention from move-between-systems). Studio creates notify-team-2, then stops notify-team.",
    );
  });

  it("finishes in the environment it was activated in when the person chooses another", async () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    const row = only(
      planFor(platform, { storedId: "t-1", declaration: WIDER }),
    );
    platform.before.set("GET /event-triggers/t-1/starts", () => {
      platform.environment = `${ENV}-elsewhere`;
    });
    expect(await apply(platform, row)).toMatchObject({ status: "replaced" });
    expect(platform.find("t-1")!["disabled"]).toBe(true);
  });

  it("stops nothing when the create fails", async () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    const row = only(
      planFor(platform, { storedId: "t-1", declaration: WIDER }),
    );
    platform.refuse.set(
      "POST /event-triggers",
      refusal(409, "WV-TRIGGER-LIMIT"),
    );
    expect(await apply(platform, row)).toMatchObject({
      status: "failed",
      retry: "create",
      text: "Couldn't create notify-team-2: This environment already has 64 platform-event triggers. Disable one first.",
    });
    expect(platform.writes()).toEqual(["POST /event-triggers"]);
    expect(platform.find("t-1")!["disabled"]).toBe(false);
  });

  it("carries the old trigger's authority to resolve transactions, and says so", async () => {
    const platform = new FakePlatform();
    platform.seed({
      id: "t-1",
      name: "notify-team",
      activation: "a-old",
      mayReconcile: true,
    });
    const row = only(
      planFor(platform, { storedId: "t-1", declaration: WIDER }),
    );
    expect(row).toMatchObject({
      kind: "replace",
      reconciles: true,
      request: { may_reconcile: true },
    });
    expect(row.text).toMatch(
      / Its runs may resolve transactions, as you after the move\. This needs permission to reconcile transactions\.$/,
    );
    const refused = await apply(platform, row);
    expect(refused).toMatchObject({
      status: "failed",
      text: "Couldn't create notify-team-2: you need to manage triggers, start runs, read runs and reconcile transactions in local.",
    });
    platform.capabilities.add("transaction.reconcile");
    expect(await apply(platform, row)).toMatchObject({ status: "replaced" });
    expect(platform.enabled()).toMatchObject([{ may_reconcile: true }]);
  });

  it("never sets may_reconcile on a create", () => {
    const platform = new FakePlatform();
    platform.seed({
      id: "t-9",
      name: "other",
      activation: "a-other",
      mayReconcile: true,
    });
    expect(only(planFor(platform))).toMatchObject({
      kind: "create",
      request: { may_reconcile: false },
    });
  });
});

describe("which triggers are this workflow's", () => {
  it.each([
    ["another workflow's name", { workflow_name: "sync-references" }],
    ["another workflow's activation", { activation_id: "a-other" }],
    ["an activation Studio doesn't know", { activation_id: "a-gone" }],
  ])("never matches or offers to stop a trigger with %s", (_name, change) => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    Object.assign(platform.find("t-1")!, change);
    platform.find("t-1")!["name"] = "other-name";
    const rows = planFor(platform, { storedId: "t-1" });
    expect(rows).toMatchObject([{ kind: "create", name: "notify-team" }]);
    expect(planFor(platform, { declaration: null })).toEqual([]);
  });

  it("ignores a stopped trigger", () => {
    const platform = new FakePlatform();
    platform.seed({
      id: "t-1",
      name: "notify-team",
      activation: "a-old",
      disabled: true,
    });
    expect(planFor(platform, { storedId: "t-1" })).toMatchObject([
      { kind: "create", name: "notify-team" },
    ]);
  });

  it("blocks a name-only match with other events and sends nothing for it", async () => {
    const platform = new FakePlatform();
    platform.seed({
      id: "t-cli",
      name: "notify-team",
      activation: "a-old",
      events: ["transaction.in_doubt", "transaction.compensated"],
    });
    const row = only(planFor(platform));
    expect(row).toMatchObject({
      kind: "blocked",
      reason: "outside-studio",
      binding: { id: "t-cli" },
      text: "notify-team was created outside Studio with other events or source workflows. Studio leaves it as it is.",
    });
    expect(eventRowActionable(row)).toBe(false);
    await expect(apply(platform, row)).rejects.toThrow();
    expect(platform.sent).toEqual([]);
  });

  it("moves a name-only match with equal events", () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-cli", name: "notify-team", activation: "a-old" });
    expect(only(planFor(platform))).toMatchObject({
      kind: "retarget",
      byName: true,
      binding: { id: "t-cli" },
      text: "Move platform event trigger notify-team from 1.1.0 to 1.2.0. Runs start as you after the move, including events waiting to start.",
    });
  });

  it("moves a Studio-named trigger equal to the declaration when no ID is stored", async () => {
    // A replace made notify-team-2 and stopped notify-team on another computer.
    const platform = new FakePlatform();
    platform.seed({
      id: "t-1",
      name: "notify-team",
      activation: "a-older",
      disabled: true,
    });
    platform.seed({ id: "t-2", name: "notify-team-2", activation: "a-old" });
    const row = only(planFor(platform));
    expect(row).toMatchObject({
      kind: "retarget",
      byName: true,
      binding: { id: "t-2" },
      text: "Move platform event trigger notify-team-2 from 1.1.0 to 1.2.0. Runs start as you after the move, including events waiting to start.",
    });
    expect(await apply(platform, row)).toMatchObject({
      status: "moved",
      id: "t-2",
    });
    expect(platform.enabled()).toMatchObject([
      { id: "t-2", activation_id: "a-new" },
    ]);
  });

  it("moves the trigger named after the workflow first when two react as declared", () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team-2", activation: "a-older" });
    platform.seed({ id: "t-2", name: "notify-team", activation: "a-old" });
    expect(planFor(platform)).toMatchObject([
      { kind: "retarget", binding: { id: "t-2" } },
      { kind: "duplicate", binding: { id: "t-1" }, stop: true },
    ]);
  });

  it("moves the Studio-named trigger equal to the declaration before the one named after the workflow", () => {
    // notify-team's stop is owed on another computer; notify-team-2 replaced it there.
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-older" });
    platform.seed({
      id: "t-2",
      name: "notify-team-2",
      activation: "a-old",
      events: ["transaction.compensated", "transaction.in_doubt"],
      sources: undefined,
    });
    expect(planFor(platform, { declaration: WIDER })).toMatchObject([
      { kind: "retarget", byName: true, binding: { id: "t-2" } },
      {
        kind: "left-behind",
        binding: { id: "t-1" },
        stop: false,
        text: "Platform event trigger notify-team keeps running on 1.0.0",
      },
    ]);
  });

  it("leaves every trigger behind, off, for a version without eventTrigger", () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    platform.seed({ id: "t-2", name: "notify-team-2", activation: "a-older" });
    expect(planFor(platform, { declaration: null, storedId: "t-1" })).toEqual([
      expect.objectContaining({
        kind: "left-behind",
        binding: expect.objectContaining({ id: "t-1" }),
        stop: false,
        text: "Platform event trigger notify-team keeps running on 1.1.0",
      }),
      expect.objectContaining({
        kind: "left-behind",
        binding: expect.objectContaining({ id: "t-2" }),
        stop: false,
        text: "Platform event trigger notify-team-2 keeps running on 1.0.0",
      }),
    ]);
  });

  it("leaves another trigger of the workflow with other events behind, off", () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    platform.seed({
      id: "t-2",
      name: "notify-old",
      activation: "a-older",
      events: ["transaction.completed"],
    });
    expect(planFor(platform, { storedId: "t-1" })).toMatchObject([
      { kind: "retarget", binding: { id: "t-1" } },
      {
        kind: "left-behind",
        binding: { id: "t-2" },
        from: "1.0.0",
        stop: false,
      },
    ]);
  });
});

describe("a trigger whose authority to resolve is unknown", () => {
  it.each([
    ["found by its stored ID", "t-1", ATTENTION],
    ["found by its stored ID, with other events", "t-1", WIDER],
    ["found by name", undefined, ATTENTION],
  ])(
    "is blocked when %s, and nothing is sent",
    async (_name, storedId, declaration) => {
      const platform = new FakePlatform();
      platform.seed({
        id: "t-1",
        name: "notify-team",
        activation: "a-old",
        mayReconcile: null,
      });
      const row = only(planFor(platform, { storedId, declaration }));
      expect(row).toMatchObject({
        kind: "blocked",
        reason: "unknown-reconcile",
        text: "Studio can't tell whether notify-team may resolve transactions, so it leaves it as it is.",
      });
      expect(eventRowActionable(row)).toBe(false);
      await expect(apply(platform, row)).rejects.toThrow();
      expect(platform.sent).toEqual([]);
    },
  );

  it("still shows a second trigger reacting as it does as a duplicate, on", () => {
    const platform = new FakePlatform();
    platform.seed({
      id: "t-1",
      name: "notify-team",
      activation: "a-old",
      mayReconcile: null,
    });
    platform.seed({ id: "t-2", name: "notify-team-2", activation: "a-older" });
    expect(planFor(platform, { storedId: "t-1" })).toMatchObject([
      { kind: "blocked", reason: "unknown-reconcile" },
      { kind: "duplicate", binding: { id: "t-2" }, stop: true },
    ]);
  });
});

describe("two triggers reacting to the same events", () => {
  it("shows a second trigger of the workflow with the stored trigger's events as a duplicate, on", () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    platform.seed({ id: "t-2", name: "notify-team-2", activation: "a-older" });
    expect(planFor(platform, { storedId: "t-1" })).toMatchObject([
      { kind: "retarget", binding: { id: "t-1" } },
      {
        kind: "duplicate",
        binding: { id: "t-2" },
        from: "1.0.0",
        stop: true,
        text: "Two triggers start notify-team for the same events. Stop notify-team-2",
      },
    ]);
  });

  it("shows a trigger already equal to the new declaration as a duplicate, on, during a replace", async () => {
    // An earlier replace made notify-team-2, but its answer was lost and nobody pressed Retry.
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    platform.seed({
      id: "t-2",
      name: "notify-team-2",
      activation: "a-old",
      events: ["transaction.compensated", "transaction.in_doubt"],
      sources: undefined,
    });
    const rows = planFor(platform, { storedId: "t-1", declaration: WIDER });
    expect(rows).toMatchObject([
      { kind: "replace", binding: { id: "t-1" }, name: "notify-team-3" },
      {
        kind: "duplicate",
        binding: { id: "t-2" },
        from: "1.1.0",
        stop: true,
        text: "Two triggers start notify-team for the same events. Stop notify-team-2",
      },
    ]);
    for (const row of rows) await apply(platform, row);
    expect(platform.enabled()).toMatchObject([
      { name: "notify-team-3", activation_id: "a-new" },
    ]);
  });

  it("offers to stop a trigger Studio didn't name that equals the declaration it creates", () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-ops", name: "ops-alerts", activation: "a-old" });
    expect(planFor(platform)).toMatchObject([
      { kind: "create", name: "notify-team" },
      { kind: "duplicate", binding: { id: "t-ops" }, stop: true },
    ]);
  });

  it("never offers to stop the only trigger reacting as declared", () => {
    // Studio leaves notify-team as it is, so ops-alerts is the one that reacts as declared.
    const platform = new FakePlatform();
    platform.seed({
      id: "t-cli",
      name: "notify-team",
      activation: "a-old",
      events: ["transaction.completed"],
    });
    platform.seed({ id: "t-ops", name: "ops-alerts", activation: "a-older" });
    expect(planFor(platform)).toMatchObject([
      { kind: "blocked", reason: "outside-studio", binding: { id: "t-cli" } },
      { kind: "left-behind", binding: { id: "t-ops" }, stop: false },
    ]);
  });

  it("shows a stop Studio still owes as a duplicate, on", () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team-2", activation: "a-old" });
    platform.seed({
      id: "t-old",
      name: "notify-team",
      activation: "a-older",
      events: ["transaction.completed"],
      revision: 4,
    });
    const owed: PendingStop = {
      triggerId: "t-old",
      revision: 4,
      activationId: "a-older",
    };
    expect(
      planFor(platform, { storedId: "t-1", pendingStops: [owed] }),
    ).toMatchObject([
      { kind: "retarget" },
      { kind: "duplicate", binding: { id: "t-old" }, stop: true },
    ]);
  });

  it("forgets an owed stop once someone changed that trigger", () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team-2", activation: "a-old" });
    platform.seed({
      id: "t-old",
      name: "notify-team",
      activation: "a-older",
      events: ["transaction.completed"],
      revision: 5,
    });
    expect(
      planFor(platform, {
        storedId: "t-1",
        pendingStops: [
          { triggerId: "t-old", revision: 4, activationId: "a-older" },
        ],
      }),
    ).toMatchObject([
      { kind: "retarget" },
      { kind: "left-behind", binding: { id: "t-old" }, stop: false },
    ]);
  });

  it("records a pending stop when the stop fails", async () => {
    const platform = new FakePlatform();
    platform.seed({
      id: "t-1",
      name: "notify-team",
      activation: "a-old",
      revision: 2,
    });
    const row = only(
      planFor(platform, { storedId: "t-1", declaration: WIDER }),
    );
    platform.refuse.set(
      "POST /event-triggers/t-1/disable",
      refusal(500, "WV-INTERNAL", "Request failed"),
    );
    expect(await apply(platform, row)).toMatchObject({
      status: "failed",
      id: "t-made-1",
      retry: "stop",
      text: "Created notify-team-2, but couldn't stop notify-team: Request failed.",
      pendingStop: { triggerId: "t-1", revision: 2, activationId: "a-old" },
    });
    expect(platform.enabled()).toHaveLength(2);
    // Stop it: the created trigger is adopted, not created twice, and the stop clears what was owed.
    expect(await apply(platform, row)).toMatchObject({
      status: "replaced",
      id: "t-made-1",
      pendingStop: null,
    });
    expect(platform.enabled().map((trigger) => trigger["id"])).toEqual([
      "t-made-1",
    ]);
  });

  it("clears the owed stop when a duplicate is stopped", async () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    platform.seed({ id: "t-2", name: "notify-team-2", activation: "a-older" });
    const duplicate = planFor(platform, { storedId: "t-1" })[1]!;
    expect(await apply(platform, duplicate)).toMatchObject({
      status: "stopped",
      text: "Stopped notify-team-2",
      pendingStop: null,
    });
    expect(platform.find("t-2")!["disabled"]).toBe(true);
  });
});

describe("starts still waiting before a stop", () => {
  it("holds the stop while starts wait, until the person says Stop now", async () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    platform.starts.set("t-1", [requested("s-1")]);
    const row = only(
      planFor(platform, { storedId: "t-1", declaration: WIDER }),
    );
    expect(await apply(platform, row)).toMatchObject({
      status: "waiting",
      id: "t-made-1",
      text: "Created notify-team-2. notify-team has 1 event waiting to start. Stopping it now skips them.",
      pendingStop: { triggerId: "t-1", revision: 1, activationId: "a-old" },
    });
    expect(platform.writes()).toEqual(["POST /event-triggers"]);
    expect(await apply(platform, row, { stopWithWaiting: true })).toMatchObject(
      { status: "replaced", id: "t-made-1", pendingStop: null },
    );
    expect(platform.writes()).toEqual([
      "POST /event-triggers",
      "POST /event-triggers/t-1/disable",
    ]);
  });

  it("asks first when it can't read the starts", async () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    platform.seed({ id: "t-2", name: "notify-team-2", activation: "a-older" });
    const duplicate = planFor(platform, { storedId: "t-1" })[1]!;
    platform.capabilities.delete("run.read");
    expect(await apply(platform, duplicate)).toMatchObject({
      status: "waiting",
      text: "Studio can't check whether notify-team-2 has events waiting to start.",
      pendingStop: { triggerId: "t-2" },
    });
    expect(platform.writes()).toEqual([]);
  });

  it("counts the waiting starts of a duplicate before stopping it", async () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    platform.seed({ id: "t-2", name: "notify-team-2", activation: "a-older" });
    platform.starts.set("t-2", [
      requested("s-1"),
      { ...requested("s-2"), status: "started", run_id: "r-9" },
      requested("s-3"),
    ]);
    const duplicate = planFor(platform, { storedId: "t-1" })[1]!;
    expect(await apply(platform, duplicate)).toMatchObject({
      status: "waiting",
      text: "notify-team-2 has 2 events waiting to start. Stopping it now skips them.",
    });
    expect(platform.writes()).toEqual([]);
  });

  it("finds a start waiting past the first page of starts", async () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    platform.seed({ id: "t-2", name: "notify-team-2", activation: "a-older" });
    // Newest first: fifty starts that started, then an older one still waiting.
    platform.starts.set("t-2", [...startedRuns(50), requested("s-old")]);
    const duplicate = planFor(platform, { storedId: "t-1" })[1]!;
    expect(await apply(platform, duplicate)).toMatchObject({
      status: "waiting",
      text: "notify-team-2 has 1 event waiting to start. Stopping it now skips them.",
    });
    expect(platform.writes()).toEqual([]);
  });

  it("asks first when ten pages of starts hold none waiting and more remain", async () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    platform.seed({ id: "t-2", name: "notify-team-2", activation: "a-older" });
    platform.starts.set("t-2", startedRuns(501));
    const duplicate = planFor(platform, { storedId: "t-1" })[1]!;
    expect(await apply(platform, duplicate)).toMatchObject({
      status: "waiting",
      text: "Studio can't check whether notify-team-2 has events waiting to start.",
    });
    expect(
      platform.sent.filter(
        (call) => call.key === "GET /event-triggers/t-2/starts",
      ),
    ).toHaveLength(10);
    expect(platform.writes()).toEqual([]);
  });
});

describe("every stop reads the trigger again first", () => {
  type Case = [string, (platform: FakePlatform) => EventRow];
  const cases: Case[] = [
    [
      "the replaced trigger",
      (platform) => {
        platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
        return only(planFor(platform, { storedId: "t-1", declaration: WIDER }));
      },
    ],
    [
      "a duplicate",
      (platform) => {
        platform.seed({
          id: "t-0",
          name: "notify-team-2",
          activation: "a-older",
        });
        platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
        return planFor(platform, { storedId: "t-0" })[1]!;
      },
    ],
    [
      "a trigger left behind",
      (platform) => {
        platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
        return {
          ...only(planFor(platform, { declaration: null })),
          stop: true,
        } as EventRow;
      },
    ],
  ];

  it.each(cases)(
    "stops nothing and owes nothing when %s has a newer revision",
    async (_name, rowOf) => {
      const platform = new FakePlatform();
      const row = rowOf(platform);
      // Someone else moved it after the dialog read it.
      platform.before.set("GET /event-triggers/t-1", () => {
        platform.find("t-1")!["revision"] = 2;
      });
      const result = await apply(platform, row);
      expect(result.status).toBe("changed");
      expect(result.text).toMatch(
        /notify-team changed since you opened this\. Review it\.$/,
      );
      // The person reviews the changed trigger; Studio no longer owes its stop.
      expect(result.pendingStop).toBeNull();
      expect(platform.writes()).not.toContain(
        "POST /event-triggers/t-1/disable",
      );
      expect(platform.find("t-1")!["disabled"]).toBe(false);
    },
  );

  it.each(cases)(
    "stops nothing when %s now starts another workflow",
    async (_name, rowOf) => {
      const platform = new FakePlatform();
      const row = rowOf(platform);
      platform.before.set("GET /event-triggers/t-1", () => {
        platform.find("t-1")!["workflow_name"] = "sync-references";
      });
      expect((await apply(platform, row)).status).toBe("changed");
      expect(platform.find("t-1")!["disabled"]).toBe(false);
    },
  );

  it.each([
    [
      "stopped",
      (platform: FakePlatform) =>
        Object.assign(platform.find("t-1")!, { disabled: true, revision: 2 }),
    ],
    [
      "removed",
      (platform: FakePlatform) =>
        platform.triggers.splice(
          platform.triggers.indexOf(platform.find("t-1")!),
          1,
        ),
    ],
  ])(
    "counts a trigger someone already %s as stopped and clears the owed stop",
    async (_name, change) => {
      const platform = new FakePlatform();
      platform.seed({ id: "t-0", name: "notify-team-2", activation: "a-old" });
      platform.seed({
        id: "t-1",
        name: "notify-team",
        activation: "a-older",
        events: ["transaction.completed"],
      });
      const owed: PendingStop = {
        triggerId: "t-1",
        revision: 1,
        activationId: "a-older",
      };
      const duplicate = planFor(platform, {
        storedId: "t-0",
        pendingStops: [owed],
      })[1]!;
      expect(duplicate).toMatchObject({ kind: "duplicate", stop: true });
      platform.before.set("GET /event-triggers/t-1", () => change(platform));
      expect(await apply(platform, duplicate)).toMatchObject({
        status: "stopped",
        text: "Stopped notify-team",
        pendingStop: null,
      });
      expect(platform.writes()).toEqual([]);
    },
  );

  it.each(cases)(
    "stops nothing when %s runs another activation",
    async (_name, rowOf) => {
      const platform = new FakePlatform();
      const row = rowOf(platform);
      platform.before.set("GET /event-triggers/t-1", () => {
        platform.find("t-1")!["activation_id"] = "a-older";
      });
      expect((await apply(platform, row)).status).toBe("changed");
      expect(platform.writes()).not.toContain(
        "POST /event-triggers/t-1/disable",
      );
    },
  );

  it.each(cases)(
    "stops nothing when %s moves between the read and the stop",
    async (_name, rowOf) => {
      const platform = new FakePlatform();
      const row = rowOf(platform);
      // Someone moved it after Studio read it again, before the stop.
      platform.before.set("POST /event-triggers/t-1/disable", () => {
        const trigger = platform.find("t-1")!;
        trigger["activation_id"] = "a-older";
        trigger["revision"] = 2;
      });
      const result = await apply(platform, row);
      expect(result.status).toBe("changed");
      expect(result.text).toMatch(
        /notify-team changed since you opened this\. Review it\.$/,
      );
      expect(result.retry).toBeUndefined();
      expect(platform.find("t-1")!["disabled"]).toBe(false);
    },
  );

  it("stops a trigger left behind when the person ticks Stop it", async () => {
    const platform = new FakePlatform();
    platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
    const row = only(planFor(platform, { declaration: null }));
    expect(eventRowActionable(row)).toBe(false);
    const ticked = { ...row, stop: true } as EventRow;
    expect(eventRowActionable(ticked)).toBe(true);
    expect(await apply(platform, ticked)).toMatchObject({
      status: "stopped",
      text: "Stopped notify-team",
    });
    expect(platform.sent.map((call) => call.key)).toEqual([
      "GET /event-triggers/t-1/starts",
      "GET /event-triggers/t-1",
      "POST /event-triggers/t-1/disable",
    ]);
  });

  it.each([
    [
      "has events waiting to start",
      (platform: FakePlatform) =>
        platform.starts.set("t-1", [requested("s-1")]),
      "waiting",
    ],
    [
      "couldn't be stopped",
      (platform: FakePlatform) =>
        platform.refuse.set(
          "POST /event-triggers/t-1/disable",
          refusal(500, "WV-INTERNAL", "Request failed"),
        ),
      "failed",
    ],
  ])(
    "owes no stop for a trigger left behind that %s",
    async (_name, fault, status) => {
      // It reacts to events the new version doesn't, so keeping it starts nothing twice.
      const platform = new FakePlatform();
      platform.seed({ id: "t-1", name: "notify-team", activation: "a-old" });
      const ticked = {
        ...only(planFor(platform, { declaration: null })),
        stop: true,
      } as EventRow;
      fault(platform);
      const result = await apply(platform, ticked);
      expect(result.status).toBe(status);
      expect(result).not.toHaveProperty("pendingStop");
    },
  );
});
