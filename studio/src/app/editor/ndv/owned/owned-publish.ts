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
// Publishes the actions a workflow owns before the workflow, or a
// simulation, needs them. One request per action, in step order, each
// checked against the platform first when the account may compile. The
// platform keeps every version as it is, so a publish that stops partway
// leaves only finished, unused versions behind, and trying again sends the
// same request: an action already published with the same content comes back
// as it is.
import { signal } from "@angular/core";
import { stringify } from "yaml";
import { describeDiagnostic } from "../../../designer/diagnostic-copy";
import { describeError } from "../../../errors";
import type {
  HostDiagnostic,
  PlatformCompileResult,
  PublishedVersion,
} from "../../../integrations/http-action-client";
import {
  classifyPublishError,
  needsConnector,
  nextPatchVersion,
  versionTaken,
  type DiagnosticLike,
} from "../../../integrations/http-action-model";
import type { StructuredCanvasAdapter } from "../../../model";
import { uniqueIdentifier } from "../params/identifiers";
import { allSteps } from "../params/slots";
import type {
  OwnedPublishOutcome,
  OwnedPublishPurpose,
} from "../step-details-service";
import { takenNames } from "./insert-owned";
import { nameOfUses, ownedUses, versionOfUses } from "./owned-actions";
import { ownedActionsOf } from "./owned-store";
import {
  driftedOwned,
  forkEdited,
  markPublished,
  moveOwned,
  pendingOwned,
  publishKey,
} from "./owned-versions";

export type OwnedRowState =
  | "checking"
  | "ready"
  | "conflict"
  | "blocked"
  | "publishing"
  | "published"
  | "failed"
  | "unknown";
export type OwnedPublishPhase =
  | "preparing"
  | "confirm"
  | "blocked"
  | "publishing"
  | "partial"
  | "done";
export interface OwnedRow {
  /** The action as the steps call it now: `name@version`. */
  uses: string;
  name: string;
  version: string;
  /** The steps that call it, in step order. */
  steps: string[];
  state: OwnedRowState;
  /** What to know about this row, in plain words; "" when nothing. */
  message: string;
  /** For a name and version taken by other content: the action Studio publishes instead. */
  proposal?: string;
}
/** What the publisher needs from step details and the platform. */
export interface OwnedPublishPorts {
  readonly model: StructuredCanvasAdapter;
  readonly purpose: OwnedPublishPurpose;
  editingLocked(): boolean;
  canCompile(): boolean;
  /** Versions of an action name the workspace lists (the loaded catalog pages). */
  versionsOf(name: string): readonly string[];
  /** Names a new owned action must not take. */
  reserved(): ReadonlySet<string>;
  sync(): void;
  settled(): Promise<void>;
  document(uses: string): Record<string, unknown> | null;
  problems(): ReadonlyMap<string, readonly HostDiagnostic[]>;
  compile(document: Record<string, unknown>): Promise<PlatformCompileResult>;
  publish(yaml: string, key: string): Promise<PublishedVersion>;
  /** Runs an edit as one undo step; false when it was refused (the shell says why). */
  perform(edit: () => void): boolean;
  /** Records a publication in the last undo step, leaving the save state alone. */
  record(edit: () => void): boolean;
}

const count = (n: number): string => `${n} ${n === 1 ? "action" : "actions"}`;
const callers = (model: StructuredCanvasAdapter, uses: string): string[] =>
  allSteps(model.definition)
    .filter((step) => step["uses"] === uses)
    .map((step) => step.id);
const firstError = (
  diagnostics: readonly DiagnosticLike[] | undefined,
): DiagnosticLike =>
  diagnostics?.find((d) => d.severity === "error") ?? { message: "" };

/** The connector an Action document needs, in words: "the connector weave-email 1.0.0". */
function connectorText(document: Record<string, unknown> | null): string {
  const spec = (document?.["spec"] ?? {}) as Record<
    string,
    Record<string, unknown> | undefined
  >;
  const connector = String(
    spec["connection"]?.["connector"] ?? spec["implementation"]?.["uses"] ?? "",
  );
  const named = connector
    ? `the connector ${connector.replace("@", " ")}`
    : "the connector this action needs";
  return `This project doesn't have ${named} yet. Ask a developer to publish it, then check again.`;
}

/**
 * Where a taken action goes: one this workflow published before moves to its
 * next free patch version; one it never published moves to a free name, as a
 * new owned action would get.
 */
export function proposeMove(
  ports: Pick<OwnedPublishPorts, "model" | "versionsOf" | "reserved">,
  uses: string,
  rows: readonly OwnedRow[],
): string {
  const recipe = ownedActionsOf(ports.model.canvas)[uses];
  const name = nameOfUses(uses);
  const version = versionOfUses(uses);
  const proposed = rows
    .map((row) => row.proposal)
    .filter((p): p is string => !!p);
  if (recipe?.published)
    return ownedUses(
      name,
      nextPatchVersion(version, [
        ...ports.versionsOf(name),
        recipe.published.version,
        version,
        ...proposed.filter((p) => nameOfUses(p) === name).map(versionOfUses),
      ]),
    );
  const taken = new Set([
    ...takenNames(ports.model, ports.reserved()),
    ...proposed.map(nameOfUses),
    name,
  ]);
  return ownedUses(uniqueIdentifier(name, taken), version);
}

export class OwnedPublisher {
  readonly rows = signal<OwnedRow[]>([]);
  readonly phase = signal<OwnedPublishPhase>("preparing");
  readonly summary = signal("");
  private published = 0;
  /** A fresh key for a row whose content-derived key the platform saw with other content. */
  private readonly keys = new Map<string, string>();

  constructor(private readonly ports: OwnedPublishPorts) {}

  /** Publishing is under way: the dialog can't close now. */
  busy(): boolean {
    return this.phase() === "publishing";
  }
  outcome(): OwnedPublishOutcome {
    const phase = this.phase();
    return {
      done: phase === "done",
      published: this.published,
      ...(phase === "partial" || phase === "blocked"
        ? { reason: this.summary() }
        : {}),
    };
  }

  /** Lists what to publish: changed published actions move to their next version first, then every row is checked. */
  async prepare(): Promise<void> {
    const { model } = this.ports;
    this.phase.set("preparing");
    this.summary.set("");
    if (model.readonly) return this.stop("Fix the workflow source first.");
    if (driftedOwned(model).length) {
      if (this.ports.editingLocked())
        return this.stop(
          "Stop the simulation first: actions that changed after they were published need new versions.",
        );
      const moved = this.ports.perform(() =>
        model.batch(() => {
          forkEdited(model, (name) => this.ports.versionsOf(name));
        }),
      );
      if (!moved)
        return this.stop(
          "Studio couldn't give the changed actions new versions.",
        );
    }
    this.ports.sync();
    await this.ports.settled();
    const owned = ownedActionsOf(model.canvas);
    this.rows.set(
      pendingOwned(model).map((uses) => {
        const published = owned[uses]?.published;
        return {
          uses,
          name: nameOfUses(uses),
          version: versionOfUses(uses),
          steps: callers(model, uses),
          state: "ready",
          // An earlier version is published: this one is its change.
          message: published
            ? `Changed after version ${published.version} was published.`
            : "",
        };
      }),
    );
    await this.check();
  }

  /** Applies the proposals, then publishes every row that isn't published yet, in order. */
  async publish(): Promise<boolean> {
    const { model } = this.ports;
    const moving = this.rows().filter((row) => row.proposal);
    if (moving.length) {
      if (this.ports.editingLocked()) {
        this.stop(
          "Stop the simulation first: taken actions need new names or versions.",
        );
        return false;
      }
      const moved = this.ports.perform(() =>
        model.batch(() => {
          for (const row of moving) moveOwned(model, row.uses, row.proposal!);
        }),
      );
      if (!moved) return false;
      this.rows.update((rows) =>
        rows.map((row) =>
          row.proposal
            ? {
                ...row,
                uses: row.proposal,
                name: nameOfUses(row.proposal),
                version: versionOfUses(row.proposal),
                state: "ready",
                message: "",
                proposal: undefined,
              }
            : row,
        ),
      );
      this.ports.sync();
      await this.ports.settled();
      // The new name or version may be taken too.
      await this.check();
      if (this.phase() !== "confirm") return false;
    }
    this.phase.set("publishing");
    const rows = this.rows();
    for (const row of rows) {
      if (row.state === "published") continue;
      const document = this.ports.document(row.uses);
      if (!document) {
        this.set(row.uses, {
          state: "blocked",
          message:
            "Studio is still preparing this action. Check again in a moment.",
        });
        return this.stopped();
      }
      const yaml = stringify(document);
      const key = this.keys.get(row.uses) ?? publishKey(row.uses, yaml);
      this.set(row.uses, { state: "publishing", message: "" });
      this.summary.set(
        `Publishing ${this.counted("published") + 1} of ${rows.length}…`,
      );
      try {
        await this.ports.publish(yaml, key);
      } catch (error) {
        this.failed(row, key, error);
        return this.stopped();
      }
      this.ports.record(() => {
        model.canvas = markPublished(model, row.uses);
      });
      this.published++;
      this.set(row.uses, { state: "published" });
    }
    this.phase.set("done");
    this.summary.set(`${count(rows.length)} published.`);
    return true;
  }

  private async check(): Promise<void> {
    for (const row of this.rows())
      if (row.state !== "published") this.checkBuilt(row);
    if (this.ports.canCompile())
      for (const row of this.rows())
        if (row.state === "ready") await this.checkOnPlatform(row);
    this.settle();
  }
  private checkBuilt(row: OwnedRow): void {
    const problem = this.ports.problems().get(row.uses)?.[0];
    if (problem)
      return this.set(row.uses, { state: "blocked", message: problem.message });
    if (this.ports.document(row.uses))
      return this.set(row.uses, { state: "ready" });
    const recipe = ownedActionsOf(this.ports.model.canvas)[row.uses];
    this.set(row.uses, {
      state: "blocked",
      message:
        recipe?.kind === "http"
          ? `Fill in the URL of step ${row.steps[0] ?? row.name} first.`
          : "Studio is still preparing this action. Check again in a moment.",
    });
  }
  private async checkOnPlatform(row: OwnedRow): Promise<void> {
    const document = this.ports.document(row.uses)!;
    this.set(row.uses, { state: "checking" });
    let result: PlatformCompileResult;
    try {
      result = await this.ports.compile(document);
    } catch (error) {
      return this.set(row.uses, {
        state: "failed",
        message: `Studio couldn't check this action. ${describeError(error).message}`,
      });
    }
    const errors = result.diagnostics.filter((d) => d.severity === "error");
    if (versionTaken(errors)) return this.taken(row);
    if (needsConnector(errors))
      return this.set(row.uses, {
        state: "blocked",
        message: connectorText(document),
      });
    if (!result.ok || errors.length)
      return this.set(row.uses, {
        state: "blocked",
        message: `The platform found a problem in this action: ${describeDiagnostic(firstError(errors)).text}`,
      });
    this.set(row.uses, { state: "ready" });
  }
  private taken(row: OwnedRow): void {
    const proposal = proposeMove(this.ports, row.uses, this.rows());
    const message =
      nameOfUses(proposal) === row.name
        ? `Version ${row.version} is already published with different content. Studio publishes this one as version ${versionOfUses(proposal)}.`
        : `Version ${row.version} of ${row.name} is already published with different content. Studio publishes this one as ${nameOfUses(proposal)} ${versionOfUses(proposal)}.`;
    this.set(row.uses, { state: "conflict", proposal, message });
  }
  private failed(row: OwnedRow, key: string, error: unknown): void {
    const failure = classifyPublishError(error);
    switch (failure.kind) {
      case "version":
        return this.taken(row);
      case "key":
        this.keys.set(
          row.uses,
          `${key.slice(0, 186)}-${crypto.randomUUID().slice(0, 12)}`,
        );
        return this.set(row.uses, {
          state: "failed",
          message:
            "The platform saw this request before with different content. Try again sends it as a new request.",
        });
      case "compile":
        return this.set(row.uses, {
          state: "blocked",
          message: needsConnector(failure.diagnostics ?? [])
            ? connectorText(this.ports.document(row.uses))
            : `The platform found a problem in this action: ${describeDiagnostic(firstError(failure.diagnostics)).text}`,
        });
      case "denied":
        return this.set(row.uses, {
          state: "blocked",
          message: "Your account can't publish actions in this workspace.",
        });
      case "unknown":
        return this.set(row.uses, {
          state: "unknown",
          message:
            "Studio couldn't confirm whether this was published. Try again sends the same request, so nothing is published twice.",
        });
      default:
        return this.set(row.uses, {
          state: "failed",
          message: `The platform refused this action: ${failure.message}`,
        });
    }
  }
  private settle(): void {
    const rows = this.rows();
    if (!rows.length) {
      this.phase.set("done");
      return;
    }
    const blocked = rows.filter(
      (row) => row.state === "blocked" || row.state === "failed",
    ).length;
    if (blocked) {
      this.phase.set("blocked");
      this.summary.set(
        `${blocked} of ${count(rows.length)} can't be published yet. ${this.counted("published") ? "Nothing more was published." : "Nothing was published."}`,
      );
      return;
    }
    this.phase.set("confirm");
    this.summary.set("");
  }
  private stopped(): false {
    const rows = this.rows();
    const done = this.counted("published");
    const stopped =
      this.ports.purpose === "publish"
        ? "The workflow wasn't published."
        : "The simulation didn't start.";
    const next = rows.some((row) => row.proposal)
      ? "Publish as suggested publishes the rest."
      : "Try again publishes the rest.";
    this.phase.set("partial");
    this.summary.set(
      `${done} of ${count(rows.length)} published. ${stopped}${done ? " What was published stays as it is." : ""} ${next}`,
    );
    return false;
  }
  private stop(message: string): void {
    this.rows.set([]);
    this.phase.set("blocked");
    this.summary.set(message);
  }
  private counted(state: OwnedRowState): number {
    return this.rows().filter((row) => row.state === state).length;
  }
  private set(uses: string, change: Partial<OwnedRow>): void {
    this.rows.update((rows) =>
      rows.map((row) => (row.uses === uses ? { ...row, ...change } : row)),
    );
  }
}
