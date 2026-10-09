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
// What each operations list shows: its columns, a row's title and secondary
// line, its status pill, and the run detail's "Now", timeline and step list.
// Pure functions: the records view renders them, tests check them.
import { durationText, relativeTime, shortId, toDate } from "../format";
import { localKeepLabel } from "../local-drafts";
import {
  runStatus,
  statusLabel,
  statusTone,
  type StatusKind,
  type Tone,
} from "../status-labels";

export type ListView = "workflows" | "runs" | "tasks" | "email" | "connections";
type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown) =>
  typeof value === "string" ? value.trim() : "";

/** What a row needs besides the record itself. */
export interface RowContext {
  /** The signed-in principal, for "Claimed by you". */
  principal?: string | null;
  /** Published versions by ID: the name and version a run belongs to. */
  versions?: ReadonlyMap<string, { name: string; version: string }>;
  /** Activations by published version ID (the Workflows status column). */
  activations?: ReadonlyMap<string, Json>;
  /** The environment's name, for "Active in Production". */
  environment?: string;
  /** Runs already read, by ID: the workflow a task's run belongs to. */
  runs?: ReadonlyMap<string, Json>;
  /** The Workflows tab the rows come from. */
  library?: "workflows" | "drafts" | "local";
  now?: Date;
}

/** Column headings per view; the first one names the row. */
export const columns: Record<ListView, string[]> = {
  workflows: ["Workflow", "Version", "Status", "Updated"],
  runs: ["Run", "Status", "Started", "Duration"],
  tasks: ["Task", "Status", "Due"],
  connections: ["Connection", "Connector", "Revision"],
  email: ["Conversation", "Last message", "Status"],
};

const nouns: Record<ListView, [string, string]> = {
  workflows: ["workflow", "workflows"],
  runs: ["run", "runs"],
  tasks: ["task", "tasks"],
  connections: ["connection", "connections"],
  email: ["conversation", "conversations"],
};
/** "3 runs", "1 task". */
export function countLabel(view: ListView, count: number) {
  const [one, many] = nouns[view];
  return `${count} ${count === 1 ? one : many}`;
}

/** "weave-http@2.0.0" reads "weave-http 2.0.0". */
export function connectorLabel(value: unknown) {
  const reference = text(value);
  const at = reference.lastIndexOf("@");
  return at > 0
    ? `${reference.slice(0, at)} ${reference.slice(at + 1)}`
    : reference;
}

/** A run's workflow and version, "expense-review 1.0.0", or "" when unknown. */
export function runWorkflow(run: Json, context: RowContext = {}) {
  const activation = isRecord(run["activation"]) ? run["activation"] : {};
  const request = isRecord(activation["request"]) ? activation["request"] : {};
  const known = context.versions?.get(text(request["version_id"]));
  const name =
    known?.name ||
    text(run["workflow_name"]) ||
    text(isRecord(run["workflow"]) ? run["workflow"]["name"] : "") ||
    text(activation["name"]);
  const version = known?.version || text(run["workflow_version"]);
  return [name, version].filter(Boolean).join(" ");
}

/** The row's first line. */
export function rowTitle(
  view: ListView,
  record: Json,
  context: RowContext = {},
) {
  if (record["unavailable"])
    return {
      runs: "Unavailable run",
      connections: "Unavailable connection",
      tasks: "Unavailable task",
      email: "Unavailable conversation",
      workflows: "Unavailable workflow",
    }[view];
  switch (view) {
    case "runs":
      return (
        runWorkflow(record, context) ||
        text(record["business_key"]) ||
        `Run ${shortId(record["id"])}`
      );
    case "tasks":
      return text(record["title"]) || "Human task";
    case "email":
      return text(record["subject"]) || "(No subject)";
    default: {
      const document = isRecord(record["document"]) ? record["document"] : {};
      const metadata = isRecord(document["metadata"])
        ? document["metadata"]
        : {};
      return (
        text(record["name"]) || text(metadata["name"]) || "untitled-workflow"
      );
    }
  }
}

/** The row's second line; never a raw UUID. */
export function rowSecondary(
  view: ListView,
  record: Json,
  context: RowContext = {},
) {
  if (record["unavailable"]) return "Studio can't show this item.";
  switch (view) {
    case "runs": {
      const key = text(record["business_key"]);
      const short = `Run ${shortId(record["id"])}`;
      return runWorkflow(record, context) ? key || short : key ? short : "";
    }
    case "tasks": {
      const run = context.runs?.get(text(record["run_id"]));
      return (
        (run ? runWorkflow(run, context) : "") ||
        text(record["workflow_name"]) ||
        `Step ${text(record["node_id"]) || "unknown"}`
      );
    }
    case "connections":
      return connectorLabel(record["connector"]);
    case "email":
      return (
        text(record["correspondent"]) ||
        text(record["from"]) ||
        text(record["sender"])
      );
    case "workflows": {
      if (context.library === "local") return localKeepLabel();
      if (context.library === "drafts") return "Not published yet";
      const activation = context.activations?.get(text(record["id"]));
      return activation
        ? `Active in ${context.environment || "this environment"}`
        : "Not activated";
    }
  }
}

export interface Pill {
  label: string;
  tone: Tone;
}
const pill = (kind: StatusKind, value: unknown, mine = false): Pill => ({
  label: statusLabel(kind, value, { mine }),
  tone: statusTone(kind, value),
});

/** The status pill, or null where the view has no status. */
export function rowStatus(
  view: ListView,
  record: Json,
  context: RowContext = {},
): Pill | null {
  switch (view) {
    case "runs":
      return pill("run", runStatus(record));
    case "tasks":
      return pill(
        "task",
        record["status"],
        !!context.principal && record["claimant_id"] === context.principal,
      );
    case "email": {
      const state = text(record["last_state"]) || text(record["state"]);
      return state ? pill("email", state) : null;
    }
    case "connections":
      return record["unavailable"] ? pill("connection", "unavailable") : null;
    case "workflows":
      if (context.library === "local")
        return { label: "Local draft", tone: "neutral" };
      if (context.library === "drafts")
        return { label: "Draft", tone: "neutral" };
      return context.activations?.has(text(record["id"]))
        ? { label: "Active", tone: "success" }
        : { label: "Published", tone: "neutral" };
  }
}

/** When a run started: its first event or creation time. */
export function runStarted(run: Json) {
  return run["started_at"] ?? run["created_at"] ?? null;
}

/** "2 min 5 s" from start to finish (or to now while it runs). */
export function runDuration(run: Json, now: Date = new Date()) {
  const start = toDate(runStarted(run));
  if (!start) return "";
  const status = runStatus(run);
  const finished = toDate(run["finished_at"] ?? run["completed_at"]);
  const end =
    finished ??
    (["succeeded", "failed", "cancelled", "timed_out"].includes(status)
      ? null
      : now);
  return end ? durationText(end.getTime() - start.getTime()) : "";
}

/** "2 min ago" for a list cell; "—" when unknown. */
export function when(value: unknown, now: Date = new Date()) {
  return relativeTime(value, now) || "—";
}

// --- run detail ---------------------------------------------------------------

/** A workflow step as the run detail needs it. */
export interface RunStep {
  id: string;
  kind: string;
  assignment?: string;
}

/** The "Now" line of a run: what it waits for, and what to do about it. */
export interface RunNow {
  /** "Waiting for", "Paused", "Running"…: shown before `subject`. */
  lead: string;
  /** The bold part, for example the task title or the step. */
  subject: string;
  /** After the subject, for example ", assigned to reviewers". */
  rest: string;
  /** "Open task", "Open My tasks", or none. */
  action: "task" | "tasks" | "";
}

const stepNames: Record<string, string> = {
  wait: "Waiting for time at",
  signal: "Waiting for a signal at",
  action: "Waiting for an action to finish at",
};

/**
 * What a run is doing now. A person's task names the task and who it is
 * assigned to; otherwise the step and what it waits for.
 */
export function runNow(
  run: Json,
  steps: RunStep[],
  task: Json | null,
): RunNow | null {
  const state = isRecord(run["state"]) ? run["state"] : {};
  const status = runStatus(run);
  if (
    ["succeeded", "failed", "cancelled", "timed_out", "unavailable"].includes(
      status,
    )
  )
    return null;
  if (status === "paused")
    return {
      lead: "Paused by an operator.",
      subject: "",
      rest: "",
      action: "",
    };
  if (
    state["incident"] ||
    (isRecord(state["incidents"]) && Object.keys(state["incidents"]).length)
  )
    return null;
  if (status === "queued")
    return {
      lead: "Queued. It starts soon.",
      subject: "",
      rest: "",
      action: "",
    };
  const active = Array.isArray(state["active"])
    ? state["active"].map(String)
    : [];
  const step = steps.find((s) => active.includes(s.id));
  if (status === "running" && !step)
    return { lead: "Running.", subject: "", rest: "", action: "" };
  if (!step) return { lead: "Waiting", subject: "", rest: "", action: "" };
  if (step.kind === "humanTask") {
    const assigned = step.assignment ? `, assigned to ${step.assignment}` : "";
    return task
      ? {
          lead: "Waiting for",
          subject: text(task["title"]) || step.id,
          rest: assigned,
          action: "task",
        }
      : {
          lead: "Waiting for a person at",
          subject: step.id,
          rest: assigned,
          action: "tasks",
        };
  }
  const lead = stepNames[step.kind];
  return lead
    ? { lead, subject: step.id, rest: "", action: "" }
    : status === "running"
      ? { lead: "Running at", subject: step.id, rest: "", action: "" }
      : { lead: "Waiting", subject: "", rest: "", action: "" };
}

/** "This run stopped at {step}." and the incident text, or null. */
export function runIncident(run: Json): { step: string; text: string } | null {
  const state = isRecord(run["state"]) ? run["state"] : {};
  const incidents = isRecord(state["incidents"])
    ? Object.values(state["incidents"]).filter(isRecord)
    : [];
  const message = text(state["incident"]);
  if (!message && !incidents.length) return null;
  const active = Array.isArray(state["active"]) ? state["active"] : [];
  const step =
    text(incidents[0]?.["node_id"]) || text(active[0]) || "an unknown step";
  return {
    step,
    text:
      message ||
      (incidents[0]?.["code"]
        ? `Support code: ${text(incidents[0]["code"])}`
        : ""),
  };
}

/** Where each step of the run's workflow is. */
export type StepProgress = "live" | "done" | "unreached";
/**
 * Where a step is: where the run is now ("live"), finished ("done": it has
 * an output in the run state, or its history says it finished), or not
 * reached yet.
 */
export function stepProgress(
  run: Json,
  id: string,
  finished: ReadonlySet<string> = new Set(),
): StepProgress {
  const state = isRecord(run["state"]) ? run["state"] : {};
  const active = Array.isArray(state["active"]) ? state["active"] : [];
  if (
    active.includes(id) &&
    !["succeeded", "failed", "cancelled", "timed_out"].includes(runStatus(run))
  )
    return "live";
  const steps = isRecord(state["steps"]) ? state["steps"] : {};
  return id in steps || finished.has(id) ? "done" : "unreached";
}

const finishing = new Set([
  "task_completed",
  "completed",
  "human_completed",
  "wait_elapsed",
  "signal_received",
]);
/** Steps the run's history says finished. */
export function finishedSteps(history: unknown): Set<string> {
  const done = new Set<string>();
  const events =
    isRecord(history) && Array.isArray(history["events"])
      ? history["events"].filter(isRecord)
      : [];
  for (const event of events) {
    const type = text(event["type"]);
    if (!finishing.has(type) && !finishing.has(type.split(".").at(-1) ?? ""))
      continue;
    const data = isRecord(event["data"]) ? event["data"] : {};
    const node =
      text(data["node_id"]) || text(data["node"]) || text(event["node"]);
    if (node) done.add(node);
  }
  return done;
}

const progressWords: Record<StepProgress, string> = {
  live: "the run is here",
  done: "finished",
  unreached: "not reached",
};
/** "review: the run is here" for the step list under the graph. */
export function stepSummary(
  run: Json,
  step: RunStep,
  finished: ReadonlySet<string> = new Set(),
) {
  return `${step.id}: ${progressWords[stepProgress(run, step.id, finished)]}`;
}

/** One timeline row. */
export interface TimelineRow {
  sentence: string;
  tone: Tone;
  at: string;
  raw: Json;
}

const eventCopy: Record<string, [string, Tone]> = {
  started: ["Run started", "neutral"],
  task_completed: ["{node} finished", "success"],
  completed: ["{node} finished", "success"],
  task_failed: ["{node} failed", "danger"],
  failed: ["{node} failed", "danger"],
  signal_received: ["Signal received at {node}", "info"],
  human_completed: ["A person decided at {node}", "success"],
  created: ["Waiting for a person at {node}", "warning"],
  paused: ["Paused by an operator", "warning"],
  resumed: ["Resumed", "info"],
  wait_elapsed: ["Wait finished at {node}", "success"],
  timed_out: ["Timed out at {node}", "danger"],
  recovery_scheduled: ["Retry scheduled for {node}", "warning"],
  incident_opened: ["Stopped at {node}", "danger"],
  incident_resolved: ["Problem at {node} resolved", "success"],
  cancelled: ["Run canceled", "neutral"],
};

/**
 * Plain sentences for a run's history: "Run started", "check finished",
 * "Waiting for a person at review". Event types like "step.completed" are
 * read by their last part.
 */
export function timeline(history: unknown): TimelineRow[] {
  const events =
    isRecord(history) && Array.isArray(history["events"])
      ? history["events"].filter(isRecord)
      : [];
  return events.map((event) => {
    const type = text(event["type"]);
    const short = type.split(".").at(-1) ?? type;
    const data = isRecord(event["data"]) ? event["data"] : {};
    const node =
      text(data["node_id"]) || text(data["node"]) || text(event["node"]);
    const [template, tone] = eventCopy[type] ??
      eventCopy[short] ?? [
        short.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase()) ||
          "Event",
        "neutral" as Tone,
      ];
    const sentence = template.includes("{node}")
      ? node
        ? template.replace("{node}", node)
        : template
            .replace(/ (at|for) \{node\}|\{node\} /, "")
            .replace(/^./, (c) => c.toUpperCase())
      : template;
    return {
      // A step ID keeps its spelling: "check finished".
      sentence:
        node && template.startsWith("{node}")
          ? sentence
          : sentence.charAt(0).toUpperCase() + sentence.slice(1),
      tone,
      at: text(event["timestamp"]) || text(event["at"]),
      raw: event,
    };
  });
}

/** A task's context as label/value pairs; nested values go to Technical details. */
export function contextFacts(context: unknown): {
  facts: { label: string; value: string }[];
  nested: Json;
} {
  const facts: { label: string; value: string }[] = [];
  const nested: Json = {};
  if (!isRecord(context)) return { facts, nested };
  const currency = text(context["currency"]);
  for (const [key, value] of Object.entries(context)) {
    if (key === "currency" && typeof context["amount"] === "number") continue;
    if (value !== null && typeof value === "object") {
      nested[key] = value;
      continue;
    }
    let shown = value === null ? "—" : String(value);
    if (key === "amount" && typeof value === "number" && currency)
      try {
        shown = new Intl.NumberFormat("en-US", {
          style: "currency",
          currency,
        }).format(value);
      } catch {
        shown = `${value} ${currency}`;
      }
    facts.push({ label: fieldLabel(key), value: shown });
  }
  return { facts, nested };
}

/** "submittedBy" reads "Submitted by"; "due_at" reads "Due at". */
export function fieldLabel(key: string) {
  const words = key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replaceAll("_", " ")
    .replaceAll("-", " ")
    .trim()
    .toLowerCase();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key;
}
