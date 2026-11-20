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
// Above the Runs tab's read-only canvas: the chosen run's header (its ID,
// how it went, where it stopped, its version and its time, and what to do
// with it), the banner when it used another version, whether Studio still
// follows it, and why it can't be shown. Back to runs on a narrow screen,
// and while no run is chosen, a note that says how to choose one.
import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  effect,
  ElementRef,
  inject,
  Injector,
  input,
  signal,
  untracked,
} from "@angular/core";
import { describeError } from "../../errors";
import { durationText, relativeTime, shortId } from "../../format";
import { Icon } from "../../icon";
import { liveDuration, runOutcome } from "../../operate/runs/runs-model";
import { failedStep } from "./runs-list-model";
import { RowMenu, type RowMenuItem } from "../../row-menu";
import { statusLabel, statusTone } from "../../status-labels";
import { DebugRun, debugRefusal } from "./debug-run";
import { dropDebug, takeDebug } from "./run-links";
import { bannerSummary } from "./run-overlay";
import { OTHER_WORKFLOW } from "./runs-tab-state";
import { environmentName, type RunsView } from "./runs-view";

const FAILED = new Set(["failed", "timed_out"]);
const ENDED = new Set(["succeeded", "failed", "cancelled", "timed_out"]);
/** The run's own build part goes: "1.3.0+test.d3c2…" reads "1.3.0". */
const shortVersion = (version: string) => version.split("+")[0];
const list = (ids: readonly string[]) => ids.join(", ");

/** The open draft's test data has opened under the account (or on this computer). */
const opened = (app: { testData: { place(): { kind: string } } | null }) =>
  !!app.testData && app.testData.place().kind !== "memory";

@Component({
  selector: "weave-run-header",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, RowMenu],
  templateUrl: "./run-header.html",
  styleUrl: "./run-header.css",
  host: { class: "run-column-head" },
})
export class RunHeader {
  readonly tab = input.required<RunsView>();
  /** Ticks every second while a run is followed: its time counts up. */
  readonly now = signal(Date.now());

  readonly chosen = computed(() => this.tab().state()?.chosen() ?? null);
  /** The list has runs to choose from. */
  readonly listed = computed(() => !!this.tab().state()?.rows().length);
  readonly app = computed(() => this.tab().host());
  readonly view = computed(() => {
    const chosen = this.chosen();
    if (!chosen) return null;
    const summary = chosen.summary;
    const status = summary?.status ?? chosen.record?.status ?? null;
    const outcome = summary
      ? runOutcome(summary)
      : status
        ? { label: statusLabel("run", status), tone: statusTone("run", status) }
        : null;
    const failedAt = summary?.failed_step
      ? `Failed at ${failedStep(summary.failed_step)}${
          summary.failed_step.error_code
            ? `: ${summary.failed_step.error_code}`
            : ""
        }`
      : (() => {
          const failure = Object.entries(
            chosen.overlay?.canvas.failures ?? {},
          )[0];
          return failure ? `Failed at ${failure[0]}: ${failure[1]}` : null;
        })();
    const version = summary ? shortVersion(summary.workflow.version) : "";
    const now = new Date(this.now());
    return {
      id: chosen.id,
      short: shortId(chosen.id),
      status,
      outcome,
      loading: !chosen.record && !chosen.error,
      failed: !!status && FAILED.has(status),
      failedAt: status && FAILED.has(status) ? failedAt : null,
      version,
      older:
        !!version &&
        !summary?.test &&
        version !== this.app().model.definition.metadata.version,
      test: !!summary?.test,
      started: summary
        ? `Started ${relativeTime(summary.started_at, now)}`
        : "",
      took: !summary
        ? ""
        : status && ENDED.has(status)
          ? summary.duration_ms !== null
            ? `took ${durationText(summary.duration_ms)}`
            : ""
          : `running for ${liveDuration(summary, now)}`,
    };
  });
  /** The banner's lines: the version the run used and what changed since. */
  readonly banner = computed(() => {
    const overlay = this.chosen()?.overlay;
    if (!overlay) return [];
    const diff = overlay.diff;
    return [
      overlay.banner,
      diff?.changed.length
        ? `Changed since this run: ${list(diff.changed)}.`
        : null,
      diff?.added.length ? `New in this draft: ${list(diff.added)}.` : null,
      diff?.missing.length ? `Not in this draft: ${list(diff.missing)}.` : null,
    ].filter((line): line is string => !!line);
  });
  /** On a phone: the banner in one line, its lines behind Details. */
  readonly bannerSummary = computed(() => {
    const overlay = this.chosen()?.overlay;
    const version = this.view()?.version;
    return overlay?.banner && overlay.diff && version
      ? bannerSummary(version, overlay.diff)
      : null;
  });
  /** Why the run can't be drawn: not here, or its data withheld. */
  readonly problem = computed(() => {
    const error = this.chosen()?.error;
    if (!error) return null;
    if (error.code === OTHER_WORKFLOW)
      return {
        title: `Run ${shortId(this.chosen()!.id)} isn't a run of ${this.app().model.definition.metadata.name}.`,
        detail: "Open it in Operate to read it.",
        operate: true,
      };
    return error.code === "WV-NOT-FOUND"
      ? {
          title: `Run ${shortId(this.chosen()!.id)} isn't in ${environmentName(this.app())}.`,
          detail: "Switch to its environment to open it.",
          operate: false,
        }
      : {
          title: "Studio can't show this run's data.",
          detail: `${error.code}: ${error.message}`,
          operate: true,
        };
  });
  /** What doesn't fit beside the run's name on a narrower column. */
  readonly more = computed<RowMenuItem[]>(() => [
    { label: "Open in Operate", run: () => this.openOperate() },
    { label: "Back to editing", run: () => this.backToEditing() },
  ]);

  constructor() {
    // On a narrow screen the chosen run replaces the list: focus moves to
    // Back to runs, never staying on a row that is no longer shown.
    const injector = inject(Injector);
    const element = inject<ElementRef<HTMLElement>>(ElementRef);
    effect(() => {
      const tab = this.tab();
      if (!tab.narrow() || tab.front() !== "canvas") return;
      afterNextRender(
        () =>
          element.nativeElement
            .querySelector<HTMLElement>(".run-back button")
            ?.focus(),
        { injector },
      );
    });
    // Debug in editor or Copy to editor asked for from Operate: it starts
    // once that run has loaded here, unless Studio already knows why it can't.
    effect(() => {
      const chosen = this.chosen();
      const view = this.view();
      const refused = this.refusal();
      const access = this.tab().state()?.state() === "access";
      untracked(() => {
        const app = this.app();
        const fence = {
          scope: app.stepDataScope,
          environment: app.profile?.environmentId ?? "",
        };
        // The tab may not read runs here: nothing asked for starts later.
        // The refusal already leaves the run (refuse() calls leave(), which
        // forgets the request); this stays as a second line of defense.
        if (access) return dropDebug();
        if (!chosen) return;
        // The run's read ended in an error: the request is taken, unused.
        // Choosing another run (show()) or leaving this one (leave()) forgets
        // it as well; this stays as a second line of defense.
        if (chosen.error) return void takeDebug(app.draftId, chosen.id, fence);
        if (!chosen.record || !view || view.loading) return;
        // The draft's test data still opening: wait for it, as it may lift
        // the refusal.
        if (refused && !opened(app)) return;
        const intent = takeDebug(app.draftId, chosen.id, fence);
        if (intent && !refused) void this.debug(intent);
      });
    });
    // Each second while followed (the run's time counts), otherwise every
    // 30 seconds for "Started 2 min ago".
    const ticker = setInterval(() => {
      const now = Date.now();
      if (this.chosen()?.following || now - this.now() >= 30_000)
        this.now.set(now);
    }, 1000);
    inject(DestroyRef).onDestroy(() => clearInterval(ticker));
  }

  /**
   * Debug at work: reading or copying the run's data ("Copying run data…"),
   * or waiting for the Merge or Replace answer (the button keeps its name,
   * still unavailable).
   */
  readonly phase = signal<"copying" | "asking" | null>(null);
  readonly busy = computed(() => this.phase() !== null);
  /**
   * Why the run's data can't become test data here, known before a click.
   * The test data opens after the header draws (and again for another
   * account): its version says when to ask again.
   */
  readonly refusal = computed(() => {
    const app = this.app();
    app.testDataReady();
    app.testData?.version();
    return debugRefusal(app);
  });
  /** Debug in editor (a failed run) or Copy to editor (any other). */
  async debug(intent: "debug" | "copy"): Promise<void> {
    const chosen = this.chosen();
    if (!chosen || this.busy()) return;
    this.phase.set("copying");
    try {
      const outcome = await new DebugRun().run(
        this.app(),
        chosen,
        intent,
        (phase) => this.phase.set(phase),
      );
      if (outcome.kind === "refused")
        this.app().notify(outcome.message, undefined, "danger");
    } catch (error) {
      // A host or platform failure on the way (the host's check, a save):
      // said, never left unhandled.
      this.app().notify(describeError(error).message, undefined, "danger");
    } finally {
      this.phase.set(null);
    }
  }
  copyId(): void {
    const id = this.chosen()?.id;
    if (!id) return;
    void navigator.clipboard
      ?.writeText(id)
      .then(() => this.app().notify("Copied the run ID."))
      .catch(() => this.app().notify("Studio couldn't copy the run ID."));
  }
  openOperate(): void {
    const id = this.chosen()?.id;
    if (id) void this.app().viewRun({ id });
  }
  backToEditing(): void {
    void this.app().selectTab("Designer");
  }
  refresh(): void {
    void this.tab().state()?.refresh();
  }
}
