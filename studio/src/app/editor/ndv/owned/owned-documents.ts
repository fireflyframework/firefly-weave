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
// Keeps the Action documents of a workflow's owned actions in the editor's
// contract cache, so data suggestions, setup checks and step details see
// them: HTTP actions are built by the Studio host whenever their recipe or
// their step's input keys change; connector actions are copied from the
// built-in templates. Answers for a recipe that changed since are dropped.
// A build the host can't do, and a built-in action Studio can't provide,
// show up in `problems` for the action's `uses`, never silently.
import { signal } from "@angular/core";
import { describeError } from "../../../errors";
import type {
  HostDiagnostic,
  HttpActionBuildResult,
} from "../../../integrations/http-action-client";
import type { StructuredCanvasAdapter } from "../../../model";
import type { Json } from "../registry";
import { findStep } from "../step-walk";
import { ConnectorActions } from "./connector-actions";
import {
  connectorDocument,
  httpBuildRequest,
  nameOfUses,
  withRetry,
  type ConnectorRecipe,
  type OwnedRecipe,
} from "./owned-actions";
import { ownedActionsOf } from "./owned-store";

export interface OwnedDocumentsHost {
  readonly model: StructuredCanvasAdapter;
  cacheContract(uses: string, document: Record<string, unknown> | null): void;
  refreshView(): void;
}

/** The problem for an HTTP action the host couldn't build; it points at the URL, the field that most often needs fixing. */
const buildFailed = (cause?: unknown): HostDiagnostic => ({
  code: "WV-STUDIO-ACTION-BUILD",
  severity: "error",
  message: "Couldn't build the API action. Check the URL and try again.",
  path: "/pathTemplate",
  ...(cause === undefined ? {} : { hint: describeError(cause).message }),
});
const templateFailed = (cause: unknown): HostDiagnostic => ({
  code: "WV-STUDIO-CONNECTOR-ACTIONS",
  severity: "error",
  message: "Couldn't load the action this step uses. Try again in a moment.",
  path: "",
  hint: describeError(cause).message,
});
const templateMissing = (): HostDiagnostic => ({
  code: "WV-STUDIO-CONNECTOR-ACTION-UNKNOWN",
  severity: "error",
  message: "Studio doesn't have the action this step uses.",
  path: "",
});

export class OwnedDocuments {
  readonly problems = signal<ReadonlyMap<string, HostDiagnostic[]>>(new Map());
  /** The request (or recipe) each `uses` was last built from. */
  private sent = new Map<string, string>();
  private pending = new Set<Promise<void>>();
  /** The `uses` whose document this service put in the contract cache. */
  private cached = new Set<string>();

  constructor(
    private readonly host: OwnedDocumentsHost,
    private readonly build: (
      request: Record<string, unknown>,
    ) => Promise<HttpActionBuildResult>,
    private readonly connectors: ConnectorActions,
  ) {}

  /**
   * Builds what changed since the last call; cheap when nothing did. An
   * action that is gone, or whose recipe can't be built any more, also leaves
   * the contract cache, so a new action of the same name doesn't inherit its
   * parameters and response.
   */
  sync(): void {
    const owned = ownedActionsOf(this.host.model.canvas);
    let dropped = false;
    for (const uses of [...this.sent.keys()])
      if (!Object.hasOwn(owned, uses)) {
        // No longer owned: forget it, so an answer still on its way is dropped.
        this.sent.delete(uses);
        this.setProblems(uses, []);
        dropped = this.dropDocument(uses) || dropped;
      }
    for (const [uses, recipe] of Object.entries(owned)) {
      if (recipe.kind === "http") {
        const step = findStep(
          this.host.model.definition.spec.steps,
          (candidate) => candidate["uses"] === uses,
        );
        const request = httpBuildRequest(
          nameOfUses(uses),
          recipe,
          step?.["with"],
        );
        const key = JSON.stringify([request, recipe.retry ?? null]);
        if (this.sent.get(uses) === key) continue;
        this.sent.set(uses, key);
        if (!request) {
          this.setProblems(uses, []);
          dropped = this.dropDocument(uses) || dropped;
          continue;
        }
        this.track(this.buildHttp(uses, recipe, request, key));
      } else {
        const key = JSON.stringify(recipe);
        if (this.sent.get(uses) === key) continue;
        this.sent.set(uses, key);
        this.track(this.copyConnector(uses, recipe, key));
      }
    }
    if (dropped) this.host.refreshView();
  }
  /** Resolves once every build started so far has answered. */
  async settled(): Promise<void> {
    while (this.pending.size) await Promise.allSettled([...this.pending]);
  }

  /**
   * Failures that are expected (the host can't build, Studio can't list its
   * actions) are recorded as problems and never reject. Anything else is a
   * fault: it rejects, and surfaces as an unhandled error instead of
   * disappearing.
   */
  private track(work: Promise<void>) {
    this.pending.add(work);
    void work.finally(() => this.pending.delete(work));
  }
  private async buildHttp(
    uses: string,
    recipe: OwnedRecipe,
    request: Record<string, unknown>,
    key: string,
  ) {
    let result: HttpActionBuildResult;
    try {
      result = await this.build(request);
    } catch (error) {
      if (this.sent.get(uses) !== key) return;
      // Forget the request, so the next change builds again.
      this.sent.delete(uses);
      this.setProblems(uses, [buildFailed(error)]);
      this.host.refreshView();
      return;
    }
    if (this.sent.get(uses) !== key) return;
    const usable = result.ok && result.action;
    const errors = result.diagnostics.filter((d) => d.severity === "error");
    // A refusal that names no error still has to be seen.
    this.setProblems(uses, usable || errors.length ? errors : [buildFailed()]);
    if (usable)
      this.store(
        uses,
        withRetry(result.action as Json, recipe.retry) as Record<
          string,
          unknown
        >,
      );
    this.host.refreshView();
  }
  private async copyConnector(
    uses: string,
    recipe: ConnectorRecipe,
    key: string,
  ) {
    let template: Awaited<ReturnType<ConnectorActions["find"]>>;
    try {
      template = await this.connectors.find(recipe.connector, recipe.action);
    } catch (error) {
      if (this.sent.get(uses) !== key) return;
      // Forget the recipe, so the next change asks again.
      this.sent.delete(uses);
      this.setProblems(uses, [templateFailed(error)]);
      this.host.refreshView();
      return;
    }
    if (this.sent.get(uses) !== key) return;
    if (!template) {
      this.setProblems(uses, [templateMissing()]);
      this.host.refreshView();
      return;
    }
    this.setProblems(uses, []);
    this.store(
      uses,
      connectorDocument(nameOfUses(uses), recipe, template.document) as Record<
        string,
        unknown
      >,
    );
    this.host.refreshView();
  }
  private store(uses: string, document: Record<string, unknown>) {
    this.cached.add(uses);
    this.host.cacheContract(uses, document);
  }
  /** Takes back a document this service cached; false when it never did. */
  private dropDocument(uses: string): boolean {
    if (!this.cached.delete(uses)) return false;
    this.host.cacheContract(uses, null);
    return true;
  }
  private setProblems(uses: string, diagnostics: HostDiagnostic[]) {
    // `update` reads without tracking, so a caller inside an effect doesn't depend on `problems`.
    this.problems.update((current) => {
      const before = current.get(uses) ?? [];
      if (JSON.stringify(before) === JSON.stringify(diagnostics))
        return current;
      const next = new Map(current);
      if (diagnostics.length) next.set(uses, diagnostics);
      else next.delete(uses);
      return next;
    });
  }
}
