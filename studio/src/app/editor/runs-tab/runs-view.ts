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
// The Runs tab: the workflow's runs beside the chosen run's column (its
// header above the read-only canvas the designer projects here). It owns
// the tab's state for as long as the tab is open, starts it again when the
// account, the environment, the workflow or the right to read runs change,
// and hands the chosen run to the shell, which draws it on the canvas.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  type DoCheck,
  effect,
  inject,
  input,
  signal,
  untracked,
} from "@angular/core";
import type { App } from "../../app";
import type { RunViewsApi } from "../../operate/run-contracts";
import { RunHeader } from "./run-header";
import {
  dropDebug,
  parseRunsLink,
  replaceAddress,
  runsLink,
  takeLinkedRun,
} from "./run-links";
import { RunsList } from "./runs-list";
import { RunsTabState, type RunsTabHost } from "./runs-tab-state";

/** What the tab can show before it reads anything. */
export type { RunsTabMode } from "./runs-list-model";
import { runsTabMode, type RunsTabMode } from "./runs-list-model";

/** The shell as the tab's state reads it. */
function tabHost(app: App): RunsTabHost {
  return {
    // StudioApi reads the session's environment and project as it asks.
    api: app.api as RunViewsApi & { readonly project: string },
    scope: () => scopeOf(app),
    workflowName: () => app.model.definition.metadata.name,
    draft: () => app.model.definition,
    canRead: () => app.can("run.read"),
    environmentLabel: () => environmentName(app),
    environmentId: () => app.profile?.environmentId ?? "",
  };
}
/** The account and the workspace: a change of either forgets every run read. */
const scopeOf = (app: App) => {
  const p = app.profile;
  return `${app.stepDataScope}|${p?.tenantId ?? ""}/${p?.projectId ?? ""}/${p?.environmentId ?? ""}`;
};
/** "development"; "this environment" while its name isn't known. */
export const environmentName = (app: App) =>
  app.workspaceNames?.environment || "this environment";

const NARROW = "(max-width: 767px)";

@Component({
  selector: "weave-runs-view",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RunsList, RunHeader],
  host: {
    class: "runs-view",
    "[attr.data-front]": "front()",
  },
  styles: `
    :host {
      display: grid;
      grid-template-columns: var(--runs-list-w, 320px) minmax(0, 1fr);
      flex: 1;
      min-height: 0;
      min-width: 0;
      height: 100%;
    }
    @media (max-width: 1099px) {
      :host {
        --runs-list-w: 280px;
      }
    }
    .runs-canvas {
      position: relative;
      display: flex;
      flex-direction: column;
      min-width: 0;
      min-height: 0;
      background: var(--canvas);
    }
    .runs-canvas-area {
      position: relative;
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }
    @media (max-width: 767px) {
      :host {
        grid-template-columns: minmax(0, 1fr);
      }
      :host([data-front="list"]) .runs-canvas,
      :host([data-front="canvas"]) weave-runs-list {
        display: none;
      }
    }
  `,
  template: `<weave-runs-list [tab]="this" />
    <section class="runs-canvas" aria-label="The chosen run">
      <weave-run-header [tab]="this" />
      <div class="runs-canvas-area" (keydown)="canvasKey($event)">
        <ng-content />
      </div>
    </section>`,
})
export class RunsView implements DoCheck {
  readonly host = input.required<App>();
  /** Why the tab can't list runs yet, or "ready". */
  readonly mode = signal<RunsTabMode>("pending");
  readonly state = signal<RunsTabState | null>(null);
  /** The run the list marks as chosen (`aria-selected`). */
  readonly selected = signal<string | null>(null);
  /** Below 768 px one column shows: the list, or the chosen run. */
  readonly front = signal<"list" | "canvas">("list");
  readonly narrow = signal(false);
  /** Bumped to move focus back to the chosen row. */
  readonly focusRow = signal(0);
  private key = "";
  /** The run a link to this tab names, chosen once the tab can read runs. */
  private linked: string | null = null;
  /** The link was read: only when the tab opens, never again. */
  private linkRead = false;
  /** The newest run was shown for this list once: opening the tab shows it. */
  private opened = false;

  constructor() {
    const media = globalThis.matchMedia?.(NARROW);
    this.narrow.set(!!media?.matches);
    const changed = (event: MediaQueryListEvent) =>
      this.narrow.set(event.matches);
    media?.addEventListener("change", changed);
    inject(DestroyRef).onDestroy(() => {
      // Leaving the tab forgets a Debug request it didn't start.
      dropDebug();
      media?.removeEventListener("change", changed);
      this.state()?.destroy();
      this.release();
      // Leaving the tab drops it and its run from the address.
      replaceAddress(this.path(), this.path());
    });
    // The address names the tab and the chosen run, in place of the entry
    // the workflow opened with: choosing runs adds no history.
    effect(() => {
      const run = this.state()?.chosen()?.id ?? null;
      untracked(() =>
        replaceAddress(this.path(), runsLink(this.host().draftId, run)),
      );
    });
    // The shell knows the tab is open and whether its list has runs; it
    // draws the chosen run once its data has loaded, and until then the
    // canvas keeps the draft without a run on it.
    effect(() => {
      const state = this.state();
      const chosenRun = state?.chosen() ?? null;
      const data = chosenRun?.data ?? null;
      const listed = !!state?.rows().some((row) => !row.unavailable);
      const chosen = chosenRun?.id ?? null;
      const loading = !!chosenRun && !data && !chosenRun.error;
      untracked(() => {
        const app = this.host();
        if (!state) return this.release();
        const shown = app.runView();
        if (
          shown?.data === data &&
          shown.listed === listed &&
          shown.chosen === chosen &&
          shown.loading === loading
        )
          return;
        app.runView.set({
          canvas: () =>
            data ? (state.chosen()?.overlay?.canvas ?? null) : null,
          data,
          listed,
          chosen,
          loading,
          close: () => state.leave(),
        });
      });
    });
    // Opening the tab shows the newest run once the list loads, except on a
    // narrow screen, where the list stays in front.
    effect(() => {
      const state = this.state();
      const rows = state?.rows() ?? [];
      if (!state || this.opened || state.state() !== "ready") return;
      this.opened = true;
      const newest = rows.find((row) => !row.unavailable);
      if (newest && !untracked(this.narrow))
        untracked(() => this.show(newest.id));
    });
  }

  ngDoCheck(): void {
    const app = this.host();
    if (!this.linkRead) {
      this.linkRead = true;
      this.linked =
        takeLinkedRun(app.draftId) ??
        parseRunsLink(location.search)?.run ??
        null;
    }
    const mode = runsTabMode({
      profile: app.profile,
      identity: app.identity,
      name: app.model.definition.metadata.name ?? "",
    });
    const key = [
      mode,
      scopeOf(app),
      app.model.definition.metadata.name,
      mode === "ready" && app.can("run.read"),
    ].join("\n");
    if (key === this.key) return;
    this.key = key;
    this.restart(mode);
  }

  /** Chooses a run: marks its row, reads it and draws it on the canvas. */
  show(id: string): void {
    const state = this.state();
    if (!state) return;
    this.selected.set(id);
    if (state.chosen()?.id !== id) void state.choose(id);
    if (this.narrow()) this.front.set("canvas");
  }
  /**
   * Escape on the canvas that the canvas didn't use (nothing selected, no
   * gesture under way) goes back to the chosen run's row.
   */
  canvasKey(event: KeyboardEvent): void {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    event.preventDefault();
    this.backToRuns();
  }
  /** Back to the list on a narrow screen, focus on the chosen row. */
  backToRuns(): void {
    this.front.set("list");
    this.focusRow.update((count) => count + 1);
  }

  private restart(mode: RunsTabMode) {
    untracked(() => {
      // Another account, environment or workflow under a tab that could
      // read runs: a Debug request made before belongs to none of them.
      if (this.mode() === "ready") dropDebug();
      this.state()?.destroy();
      this.release();
      this.mode.set(mode);
      this.selected.set(null);
      this.front.set("list");
      this.opened = false;
      if (mode !== "ready") return this.state.set(null);
      const state = new RunsTabState(tabHost(this.host()));
      this.state.set(state);
      state.start();
      // A link's run, once: it is shown rather than the newest.
      const linked = this.linked;
      if (linked) {
        this.linked = null;
        this.opened = true;
        this.show(linked);
      }
    });
  }
  /** The open workflow's address, without a query. */
  private path(): string {
    return `/workflows/${encodeURIComponent(this.host().draftId)}/designer`;
  }
  /** The shell forgets the run it drew. */
  private release() {
    const app = this.host();
    if (app.runView()) app.runView.set(null);
  }
}
