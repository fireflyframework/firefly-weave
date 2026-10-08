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
// The run detail's commands: Cancel run, Retry run, Send signal and Export
// history. The pure helpers decide what each dialog offers; RunActions sends
// the commands through the shell, which keeps unknown outcomes recoverable.
// The server authorizes each command and checks the run's state again.
// A command's answer belongs to the run it was sent for: when the person has
// closed that run or opened another, the answer never changes what is on
// screen, and a short notice says what happened to the earlier run instead.
import type { App } from "../../app";
import { describeError, type PlainError } from "../../errors";
import { exportFile, exportNotice } from "../../export-file";
import { shortId } from "../../format";
import { sheetWhen } from "../../modal-sheet";
import type { Node } from "../../model";
import type { StartRunRequest } from "../../run/start-run-dialog";
import { terminalRunStatuses, type RunStatus } from "../run-contracts";

type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown) => (typeof value === "string" ? value : "");

/** A reason the platform accepts: 2,000 characters or fewer. */
export const REASON_LIMIT = 2000;
export function reasonProblem(reason: string) {
  return reason.length > REASON_LIMIT
    ? "Keep the reason to 2,000 characters or fewer."
    : "";
}

/** The run's own status, as the platform reports it in `state.status`. */
export function runState(run: Json): string {
  return text(isRecord(run["state"]) ? run["state"]["status"] : "");
}
export function isTerminal(run: Json) {
  return terminalRunStatuses.includes(runState(run) as RunStatus);
}

/** The signal name a waiting run expects, from its active signal step. */
export function waitingSignal(run: Json, nodes: readonly Node[]): string {
  const state = isRecord(run["state"]) ? run["state"] : {};
  const active = Array.isArray(state["active"])
    ? state["active"].map(String)
    : [];
  const step = nodes.find(
    (node) => node.step.kind === "signal" && active.includes(node.step.id),
  )?.step;
  return step ? text(step["name"]) : "";
}

/** A signal payload must be JSON; "" when it is. */
export function payloadProblem(payload: string) {
  try {
    JSON.parse(payload);
    return "";
  } catch {
    return 'Enter the payload as JSON, for example {"approved": true}.';
  }
}

/** Compares "1.10.0" and "1.9.0" by their numbers; labels are ignored. */
export function compareVersions(a: string, b: string): number {
  const parts = (value: string) =>
    value
      .split(/[-+]/)[0]
      .split(".")
      .map((part) => Number(part) || 0);
  const [x, y] = [parts(a), parts(b)];
  for (let i = 0; i < Math.max(x.length, y.length); i++)
    if ((x[i] ?? 0) !== (y[i] ?? 0)) return (x[i] ?? 0) - (y[i] ?? 0);
  return 0;
}

/** What Retry run offers: the run's own version and, when newer, the latest. */
export interface RetryChoices {
  activations: Json[];
  labels: Map<string, string>;
  initial: string;
}
export function retryChoices(
  run: Json,
  activations: readonly Json[],
  versions: ReadonlyMap<string, { name: string; version: string }>,
): RetryChoices | null {
  const own = isRecord(run["activation"]) ? run["activation"] : null;
  const id = text(own?.["id"]);
  if (!own || !id) return null;
  const versionOf = (activation: Json) =>
    versions.get(
      text(
        isRecord(activation["request"])
          ? activation["request"]["version_id"]
          : "",
      ),
    )?.version ?? "";
  const same = versionOf(own);
  const latest = activations
    .filter(
      (activation) =>
        !activation["retired"] &&
        activation["id"] !== id &&
        activation["name"] === own["name"] &&
        versionOf(activation),
    )
    .sort((a, b) => compareVersions(versionOf(b), versionOf(a)))[0];
  const newer =
    latest && (!same || compareVersions(versionOf(latest), same) > 0);
  const labels = new Map([
    [id, same ? `Same version (${same})` : "Same version"],
  ]);
  if (newer)
    labels.set(
      text(latest["id"]),
      `Latest active version (${versionOf(latest)})`,
    );
  return {
    activations: newer ? [own, latest] : [own],
    labels,
    initial: id,
  };
}

/** The input a retry starts from: the run's own input when it is an object. */
export function retryInput(run: Json): Json {
  const state = isRecord(run["state"]) ? run["state"] : {};
  return isRecord(state["input"]) ? state["input"] : {};
}

export function historyFileName(id: string) {
  return `run-${id}-history.json`;
}

/**
 * A refused command in plain words. The platform's own message and support
 * code stay; a refusal for lack of access also names the capability needed.
 */
export function explainRefusal(error: unknown, capability: string): PlainError {
  const plain = describeError(error);
  return plain.status === 403
    ? {
        ...plain,
        message: `You need ${capability} in this environment. ${plain.message}`,
      }
    : plain;
}

/** A message as a sentence: the platform's words may end without a period. */
const sentence = (message: string) =>
  /[.!?]$/.test(message) ? message : `${message}.`;

/** The run a command was sent for, and the workspace and person who sent it. */
export interface RunTarget {
  id: string;
  /** `…/runs/<id>`: where its commands go. */
  base: string;
  environment: string;
  identity: string;
}

/** The Retry run dialog while it is open. */
export interface RetryDialog extends RetryChoices {
  target: RunTarget;
  input: Json;
  keys: { business_key?: string; correlation_key?: string };
  failure: PlainError | null;
}

export class RunActions {
  retry: RetryDialog | null = null;
  exporting = false;
  /** The run the detail shows, which the Retry run dialog belongs to. */
  private shown = "";
  private alive = true;

  constructor(
    private readonly host: () => App,
    private readonly onChange: () => void,
  ) {}

  /** The detail shows this run now: another run's dialog does not stay. */
  follow(id: string) {
    if (id === this.shown) return;
    this.shown = id;
    this.retry = null;
  }
  /** The detail is gone; answers still say what happened, but draw nothing. */
  destroy() {
    this.alive = false;
  }
  private changed() {
    if (this.alive) this.onChange();
  }

  private run(): Json {
    return this.host().selectedRecord ?? {};
  }
  /** The environment commands go to; empty when there is none. */
  private environment() {
    try {
      return this.host().api.environment;
    } catch {
      return "";
    }
  }
  /** The run on screen as a command's destination, or null without one. */
  private target(): RunTarget | null {
    const id = text(this.run()["id"]);
    const environment = this.environment();
    if (!id || !environment) return null;
    return {
      id,
      base: `${environment}/runs/${encodeURIComponent(id)}`,
      environment,
      identity: JSON.stringify(this.host().identity),
    };
  }
  /** Whether the run a command was for is still the one open, for the same person. */
  private current(target: RunTarget) {
    const host = this.host();
    return (
      host.view === "runs" &&
      text(host.selectedRecord?.["id"]) === target.id &&
      this.environment() === target.environment &&
      JSON.stringify(host.identity) === target.identity
    );
  }

  /** Whether the open run's detail covers the page, and the banners on it. */
  private covered() {
    return (
      typeof matchMedia === "function" && matchMedia(sheetWhen.detail).matches
    );
  }

  /**
   * Shows a refusal where the person is looking: the banner for the open
   * run (and a notice too where the detail covers the banner), a danger
   * notice that starts with `failed` (which names the run) otherwise. A lost
   * answer is not a refusal: it keeps its own wording, starts with the run's
   * name only, and the notice offers Check now, which a detail may cover.
   */
  private refuse(
    error: unknown,
    capability: string,
    target: RunTarget,
    failed: string,
    lost = false,
  ) {
    const host = this.host();
    const plain = explainRefusal(error, capability);
    const open = this.current(target);
    if (open) host.fail({ plain });
    if (open && !this.covered()) return;
    const lead = lost ? `Run ${shortId(target.id)}:` : failed;
    const code = plain.code ? ` Support code: ${plain.code}.` : "";
    host.notify(
      `${lead} ${sentence(plain.message)}${code}`,
      lost
        ? { label: "Check now", run: () => void host.retryExact() }
        : undefined,
      "danger",
    );
  }

  async cancel() {
    const host = this.host();
    const target = this.target();
    if (!target || isTerminal(this.run()) || !host.can("run.cancel", target.id))
      return;
    const named = `Run ${shortId(target.id)}`;
    const values = await host.dialogs.form({
      title: "Cancel this run?",
      message:
        "Steps that already started may still finish outside Weave. Give a reason for the audit log.",
      confirmLabel: "Cancel run",
      cancelLabel: "Keep running",
      danger: true,
      fields: [
        {
          name: "reason",
          label: "Reason",
          required: true,
          multiline: true,
          validate: reasonProblem,
        },
      ],
    });
    if (!values) return;
    // The person may have left the run while the dialog was open.
    if (!this.current(target)) {
      host.notify(`${named} was not canceled: it is no longer open.`);
      return;
    }
    try {
      await host.safeMutation(
        `${target.base}/cancel`,
        "POST",
        { reason: values["reason"] },
        undefined,
        "Cancel run",
      );
      const effects =
        "Steps that already started may still finish outside Weave.";
      if (this.current(target)) {
        host.notify(`Run canceled. ${effects}`);
        await host.readDetail();
      } else host.notify(`${named} canceled. ${effects}`);
      if (host.view === "runs") void host.refresh();
    } catch (error) {
      this.refuse(
        error,
        "run.cancel",
        target,
        `${named} was not canceled.`,
        host.unknownCommand,
      );
    } finally {
      this.changed();
    }
  }

  async signal() {
    const host = this.host();
    const target = this.target();
    if (!target || !host.can("run.signal", target.id)) return;
    const named = `Run ${shortId(target.id)}`;
    const values = await host.dialogs.form({
      title: "Send a signal to this run?",
      message:
        "The run's waiting step receives it. Keep the event ID to send the same signal again safely.",
      confirmLabel: "Send signal",
      fields: [
        {
          name: "name",
          label: "Signal name",
          value: waitingSignal(this.run(), host.runNodes()),
          required: true,
          validate: (value) =>
            value.length > 100
              ? "Keep the signal name to 100 characters or fewer."
              : "",
        },
        {
          name: "eventId",
          label: "Event ID",
          value: crypto.randomUUID(),
          required: true,
          validate: (value) =>
            value.length > 200
              ? "Keep the event ID to 200 characters or fewer."
              : "",
        },
        {
          name: "payload",
          label: "Payload (JSON)",
          value: "{}",
          multiline: true,
          monospace: true,
          validate: payloadProblem,
        },
      ],
    });
    if (!values) return;
    if (!this.current(target)) {
      host.notify(`${named} did not get the signal: it is no longer open.`);
      return;
    }
    try {
      await host.safeMutation(
        `${target.base}/signals`,
        "POST",
        {
          eventId: values["eventId"],
          name: values["name"],
          payload: JSON.parse(values["payload"]),
        },
        undefined,
        "Send signal",
      );
      if (this.current(target)) {
        host.notify("Signal sent.");
        await host.readDetail();
      } else host.notify(`Signal sent to run ${shortId(target.id)}.`);
    } catch (error) {
      this.refuse(
        error,
        "run.signal",
        target,
        `${named} did not get the signal.`,
        host.unknownCommand,
      );
    } finally {
      this.changed();
    }
  }

  /** Opens Retry run with the run's version, and the latest one when newer. */
  async openRetry() {
    const host = this.host();
    const run = this.run();
    const target = this.target();
    if (!target || !isTerminal(run) || !host.can("run.retry", target.id))
      return;
    let activations: Json[] = [];
    if (host.can("catalog.read"))
      try {
        activations = (await host.api.page("activations", true)).items;
      } catch {
        // Without the list, the run's own version is still offered.
      }
    // The person may have moved on while the activations were read.
    if (!this.current(target)) return;
    const choices = retryChoices(run, activations, host.workflowVersions);
    if (!choices) {
      host.fail(
        new Error(
          "Studio can't retry this run: its activated version is not shown.",
        ),
      );
      return;
    }
    this.retry = {
      ...choices,
      target,
      input: retryInput(run),
      keys: {
        ...(text(run["business_key"])
          ? { business_key: text(run["business_key"]) }
          : {}),
        ...(text(run["correlation_key"])
          ? { correlation_key: text(run["correlation_key"]) }
          : {}),
      },
      failure: null,
    };
    this.changed();
  }

  async confirmRetry(body: StartRunRequest) {
    const host = this.host();
    const dialog = this.retry;
    if (!dialog) return;
    const { target } = dialog;
    // Still the dialog this retry was asked in, not one opened for another run.
    const same = () => this.retry?.target === target;
    try {
      const created = await host.safeMutation<Json>(
        `${target.base}/retry`,
        "POST",
        body,
        undefined,
        "Retry run",
      );
      if (same()) this.retry = null;
      const started = shortId(created["id"]);
      host.notify(
        this.current(target)
          ? `Started retry ${started}.`
          : `Started retry ${started} of run ${shortId(target.id)}.`,
        { label: "View", run: () => void host.viewRun(created) },
      );
      if (host.view === "runs") void host.refresh();
    } catch (error) {
      const plain = explainRefusal(error, "run.retry");
      const failed = `Run ${shortId(target.id)} was not retried.`;
      if (host.unknownCommand) {
        // Check now sits behind the dialog: leave the dialog so it can be used.
        if (same()) this.retry = null;
        this.refuse(error, "run.retry", target, failed, true);
      } else if (same() && this.current(target)) {
        if (plain.code === "WV-STUDIO-SESSION") host.fail(error);
        else this.retry = { ...this.retry!, failure: plain };
      } else this.refuse(error, "run.retry", target, failed);
    } finally {
      this.changed();
    }
  }

  /** Downloads the run's history export (its first 1,000 events). */
  async exportHistory() {
    const host = this.host();
    const target = this.target();
    if (!target || this.exporting) return;
    this.exporting = true;
    this.changed();
    try {
      const history = await host.api.request<Json>(
        `${target.base}/export?limit=1000`,
      );
      const name = historyFileName(target.id);
      const content = JSON.stringify(history, null, 2);
      const bound = "It holds the run's first 1,000 events.";
      const bounded = history["bounded_prefix"] === true;
      if (exportFile(name, "application/json", content) === "unconfirmed") {
        host.exportFallback = [{ name, mime: "application/json", content }];
        if (bounded)
          host.notify(`This export holds the run's first 1,000 events.`);
      } else
        host.notify(`${exportNotice([name])}${bounded ? ` ${bound}` : ""}`);
    } catch (error) {
      this.refuse(
        error,
        "run.read",
        target,
        `The history of run ${shortId(target.id)} was not exported.`,
      );
    } finally {
      this.exporting = false;
      this.changed();
    }
  }
}
