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
// The platform's event trigger routes for one environment, as the unit tests
// call them and the browser tests answer Studio with them: lists, reads,
// creates, moves and stops of triggers, and the starts each one left, refused
// as the platform refuses them.
import { ApiError } from "../src/app/api";
import type { EventTriggerView } from "../src/app/editor/activation/event-bindings";
import type { TriggerApi } from "../src/app/editor/activation/trigger-resources-api";

export const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";
const MESSAGES: Record<string, string> = {
  "WV-TRIGGER-CONFLICT":
    "Another enabled platform-event trigger has this name.",
  "WV-TRIGGER-EVENTS":
    "The workflow doesn't accept these platform events. Add them to its eventTrigger in a new version.",
  "WV-TRIGGER-LIMIT":
    "This environment already has 64 platform-event triggers. Disable one first.",
};
export const refusal = (
  status: number,
  code: string,
  message = MESSAGES[code]!,
) => new ApiError(status, { code, message });
const FORBIDDEN = () => refusal(403, "WV-FORBIDDEN", "Access denied");

export type Wire = Record<string, unknown>;
export interface Sent {
  key: string;
  revision?: number;
  body?: unknown;
}
export interface Seed {
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
export const WORKFLOW_OF = new Map([
  ["a-older", "notify-team"],
  ["a-old", "notify-team"],
  ["a-new", "notify-team"],
  ["a-unlisted", "notify-team"],
  ["a-other", "sync-references"],
]);

/** The platform's event trigger routes for one environment, with faults to inject. */
export class FakePlatform implements TriggerApi {
  /** The environment the person has chosen in Studio; the platform serves only ENV. */
  environment = ENV;
  /** Who is signed in. */
  person = "person-1";
  readonly replayGuard = {
    settled: async () => undefined,
    scope: () => this.person,
  };
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
export function view(seed: Seed & { principal?: string }): Wire {
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

export const requested = (id: string): Wire => ({
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
export const startedRuns = (count: number): Wire[] =>
  Array.from({ length: count }, (_, index) => ({
    ...requested(`s-${index}`),
    status: "started",
    run_id: `r-${index}`,
  }));
