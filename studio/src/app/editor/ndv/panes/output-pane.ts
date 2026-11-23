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
// The Output pane: the shape later steps can map (Schema), and what this
// step returns: its pinned test output, else what it returned in the last
// execution ("Simulated"), as Schema with values and JSON. Without data it
// offers Execute step and, for a step whose output can be pinned (Pin
// output, P, in the header), Set test output and Generate sample. Steps with
// no output say why.
import { isTriggerTarget } from "../../triggers/trigger-target";
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterEveryRender,
  inject,
  input,
  effect,
  signal,
  untracked,
  viewChild,
} from "@angular/core";
import { Icon } from "../../../icon";
import { DataTree } from "./data-tree";
import { IterationPicker, loopRows, type PickerLoop } from "./iteration-picker";
import { DataTable } from "./data-table";
import { leftOutText } from "../../state/iterations";
import { PathList } from "./path-list";
import { ScriptEditor } from "./script-editor";
import { pathRows, type PathRow } from "../../state/run-summary";
import { branchVariables } from "../../state/last-results";
import { tableCount } from "./table-view";
import { PinnedBanner } from "./pinned-banner";
import { FailureBanner } from "./failure-banner";
import { ErrorBanner } from "./error-banner";
import { relativeTime } from "../../../format";
import {
  SimulateFailureDialog,
  type FailureChoice,
} from "./simulate-failure-dialog";
import {
  codeSuggestions,
  failureKey,
  simulateBlock,
  simulatedOutcome,
} from "./simulate-failure";
import { RowMenu, type RowMenuItem } from "../../../row-menu";
import { actionSideEffect } from "../kinds/forms/action-forms";
import { failureOf } from "../../state/test-data-ops";
import { errorResultSource } from "../../step-blocks";
import type { ErrorEntry } from "../../state/test-data";
import { OutputEditor } from "./output-editor";
import {
  countLabel,
  sampleCount,
  filterRows,
  noOutputSentence,
  radioKey,
  savedView,
  writeView,
  defaultOutputView,
  schemaRows,
  withSamples,
  type DataView,
} from "./views";
import type { FormSession } from "../params/form-session";
import { usageLine } from "../kinds/ai-task-model";
import {
  CANT_SAMPLE,
  type StepDetailsController,
} from "../step-details-controller";
import { ndvRegistry, type Json } from "../registry";
import {
  asRunViewsApi,
  listRunSummaries,
  OMISSION_WORDS,
  type RunSummary,
} from "../../../operate/run-contracts";
import { durationText, shortId } from "../../../format";

/** Numbers each pane's answer, for its Show all button's aria-controls. */
let answerSequence = 0;
/** The most pages of a run's child runs read for one card: 20 pages of 100. */
const CHILD_PAGES = 20;
/** A child run's summary, from the pages of its parent's child runs (a call in a loop makes many). */
async function childSummary(
  api: ReturnType<typeof asRunViewsApi>,
  parent: string,
  child: string,
): Promise<RunSummary | null> {
  let cursor: string | undefined;
  for (let page = 0; page < CHILD_PAGES; page++) {
    const answer = await listRunSummaries(api, {
      caller_run_id: parent,
      include_test: true,
      limit: 100,
      ...(cursor ? { cursor } : {}),
    });
    const found = answer.items.find(
      (item): item is RunSummary =>
        item.id === child && !("unavailable" in item),
    );
    if (found) return found;
    if (!answer.next_cursor) return null;
    cursor = answer.next_cursor;
  }
  return null;
}
/** "notify-customer 1.0.0 · failed at send: SMTP_REJECTED · took 2 s". */
function childLine(run: RunSummary): string {
  const failed = run.failed_step;
  const how =
    run.status === "failed"
      ? failed
        ? `failed at ${failed.node_id}${failed.error_code ? `: ${failed.error_code}` : ""}`
        : "failed"
      : run.status === "succeeded"
        ? "completed"
        : run.status.replace("_", " ");
  return [
    `${run.workflow.name} ${run.workflow.version}`,
    how,
    run.duration_ms === null ? "" : `took ${durationText(run.duration_ms)}`,
  ]
    .filter(Boolean)
    .join(" · ");
}
import { instanceStep } from "../../state/instance-keys";
import { isRecordedRun } from "../../canvas/run-state";
import { RunStepLogs } from "./run-step-logs";
import { connectionsPort } from "../../connections/connections-port";
import type { RunningStepTest } from "../../state/step-test-service";
import type { RealResult } from "../../state/last-results";
import { omissionWords, realEndWords } from "../real-words";
import {
  filesIn,
  type FileReference,
} from "../../../forms/core/file-reference";
import { downloadFile } from "../../../forms/core/file-transfer";
import { fileValue } from "./file-value";
import { describeError } from "../../../errors";

/**
 * What Simulate a failure opens with for a step: the key it writes (the
 * item picked inside a loop's body), that item ("3") and the innermost loop
 * for its words, whether the step changes data, and the codes it suggests;
 * null when the step is gone. Execute step's "Simulate a failure of submit
 * first" opens it for another step than the one shown.
 */
export function simulateOpening(
  controller: StepDetailsController,
  stepId: string,
): {
  key: string;
  item: string | null;
  loop: string;
  changesData: boolean;
  /** The outcome its failure takes without asking; null when the dialog asks. */
  outcome: "not_started" | null;
  suggestions: ReturnType<typeof codeSuggestions>;
} | null {
  const step = controller.step(stepId);
  if (!step) return null;
  const uses = step["uses"];
  const contract =
    typeof uses === "string"
      ? (controller.kindContext().actionContract(uses) ?? null)
      : null;
  const key = controller.pinKey(stepId);
  return {
    key,
    item:
      key === stepId
        ? null
        : [...key.matchAll(/\[(\d+)\]/g)]
            .map((match) => (Number(match[1]) + 1).toLocaleString("en-US"))
            .join(" › "),
    loop:
      loopRows(controller.iterations, stepId, (id) => controller.step(id)).find(
        (row) => row.role === "Loop",
      )?.loopId ?? "",
    changesData:
      actionSideEffect(step, controller.kindContext()) === "non_idempotent",
    outcome: simulatedOutcome(step),
    suggestions: codeSuggestions(step, contract),
  };
}

@Component({
  selector: "weave-output-pane",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    Icon,
    DataTree,
    DataTable,
    PathList,
    PinnedBanner,
    FailureBanner,
    ErrorBanner,
    SimulateFailureDialog,
    RowMenu,
    OutputEditor,
    ScriptEditor,
    RunStepLogs,
    IterationPicker,
  ],
  template: `@let none = noOutput();
    @let pin = pinned();
    @let failing = failure();
    @let went = handledRun();
    @let canPin = pinnable();
    @let kept = !pin && answerKept();
    @let keepsAnswer = !pin && aiTask();
    @let rows = this.rows();
    @let shown = filter(rows);
    <header class="sd-pane-head">
      <h3 class="sd-pane-title">Output</h3>
      <div
        class="pane-views"
        role="radiogroup"
        aria-label="Output view"
        (keydown)="radioKey($event)"
      >
        @for (choice of views; track choice.id) {
          <button
            type="button"
            role="radio"
            [attr.aria-checked]="view() === choice.id"
            [attr.tabindex]="view() === choice.id ? 0 : -1"
            (click)="setView(choice.id)"
          >
            <weave-icon [name]="choice.icon" [size]="16" />{{ choice.label }}
          </button>
        }
      </div>
      <button
        type="button"
        class="icon-button pane-search-toggle"
        aria-label="Search output"
        [attr.aria-expanded]="searching()"
        (click)="toggleSearch()"
      >
        <weave-icon name="search" [size]="16" />
      </button>
    </header>
    @if (searching()) {
      <div class="pane-search-row">
        <input
          class="param-input pane-search"
          aria-label="Search output"
          placeholder="Search output"
          [value]="query()"
          (input)="query.set($any($event.target).value)"
          (keydown.escape)="closeSearch($event)"
        />
      </div>
    }
    @let ran = simulated();
    @let recorded = runView();
    @let real = recorded ? null : realOf();
    @let stop = recorded ? null : stoppedHere();
    <!-- What the data is (pills and the count) wraps; Pin, Edit and More
         output actions stay together at the end of the first row. -->
    <div class="pane-sub pane-output-sub">
      <div class="pane-sub-info">
        @if (stop) {
          <span class="pill" data-tone="warning"
            ><weave-icon name="schedule" [size]="16" />Stopped here</span
          >
          @if (stop.missing) {
            <span class="pane-stopped-why"
              >{{ stop.step }} needs test data</span
            >
          }
        } @else if (recorded) {
          <span class="pill" [attr.data-tone]="recorded.tone">
            @if (recorded.handled) {
              <weave-icon name="degraded" [size]="16" />
            }
            {{
              calledRunFailed(recorded)
                ? "Finished · the called run failed"
                : recorded.pill
            }}</span
          >
          <span class="pill pane-source-tag" data-tone="info">{{
            recorded.tag
          }}</span>
        } @else if (went) {
          <!-- It failed in the last execution and its On error went on. -->
          <span class="pill" data-tone="warning"
            ><weave-icon name="degraded" [size]="16" />Failed, continued</span
          >
          <span class="pill pane-source-tag"
            >{{ simulatedWords() }} · {{ went.when }}</span
          >
        } @else if (failing) {
          <span class="pill" data-tone="warning">{{
            failing.leftOut ? "Failure left out" : "Fails when executed"
          }}</span>
        } @else if (pin) {
          <span class="pill" data-tone="info">{{ pinWords(pin.key) }}</span>
        } @else if (real) {
          <button
            type="button"
            class="pill pane-source-tag pane-real-tag"
            data-tone="info"
            [attr.aria-label]="
              'From ' +
              real.environmentName +
              ', run ' +
              real.runId +
              '. Open it in Operate'
            "
            (click)="openRun(real.runId)"
          >
            <span class="pane-real-text"
              >From {{ real.environmentName }} · run
              <span class="pane-real-run">{{ short(real.runId) }}</span></span
            ><weave-icon name="external" [size]="16" />
          </button>
        } @else if (ran) {
          <span class="pill pane-source-tag">{{ simulatedWords() }}</span>
        } @else if (event() !== undefined && value() === undefined) {
          <span class="pill" data-tone="info">Sample</span>
        }
        @let paths = this.paths();
        <span class="pane-count">{{
          none ||
          stop ||
          detachedCall() ||
          (recorded && recorded.output.kind !== "value")
            ? ""
            : paths
              ? paths.took
              : event() !== undefined && view() === "table"
                ? tableCountOf()
                : event() !== undefined && view() !== "schema"
                  ? sampleCount(event()) + skippedText()
                  : countLabel(rows.length, query() ? shown.length : undefined)
        }}</span>
      </div>
      @let actions = moreActions();
      @if (canPin || actions) {
        <div class="pane-actions" role="group" aria-label="Output actions">
          @if (canPin) {
            <button
              type="button"
              class="icon-button pane-pin"
              [attr.aria-pressed]="!!pin || kept"
              [attr.aria-label]="
                keepsAnswer
                  ? 'Keep as test data'
                  : pin
                    ? 'Unpin output'
                    : 'Pin output'
              "
              [attr.aria-disabled]="
                pin || kept || realPinnable() ? null : 'true'
              "
              [title]="pinTitle(!!pin)"
              (click)="togglePin()"
            >
              <weave-icon [name]="pin || kept ? 'unpin' : 'pin'" [size]="16" />
            </button>
            <button
              type="button"
              class="icon-button pane-edit"
              aria-label="Edit output"
              title="Edit output"
              [attr.aria-pressed]="editing()"
              (click)="setOutput()"
            >
              <weave-icon name="edit" [size]="16" />
            </button>
          }
          @if (actions) {
            <weave-row-menu
              class="pane-more"
              label="More output actions"
              [items]="actions"
              [iconSize]="16"
            />
          }
        </div>
      }
    </div>
    @if (simulating(); as open) {
      <weave-simulate-failure-dialog
        [step]="session().target"
        [suggestions]="open.suggestions"
        [item]="open.item"
        [loop]="open.loop"
        [changesData]="open.changesData"
        [fixedOutcome]="open.outcome"
        [initial]="open.initial"
        (chosen)="simulate($event)"
        (closed)="closeSimulate()"
      />
    }
    @if (usage(); as line) {
      <!-- An AI task's model use, on a row of its own: the source and the
           controls above keep their room. -->
      <p class="pane-usage">{{ line }}</p>
    }
    @if (answerText(); as text) {
      <!-- A text answer, to read before keeping it: eight lines, then Show all. -->
      @let open = answerOpen() === text;
      <div class="pane-answer">
        <p
          #answerText
          class="pane-answer-text"
          [class.is-clamped]="!open"
          [id]="answerId"
          [textContent]="text"
        ></p>
        @if (answerLonger()) {
          <button
            type="button"
            class="text-link pane-answer-more"
            [attr.aria-expanded]="open"
            [attr.aria-controls]="answerId"
            (click)="toggleAnswer(text)"
          >
            {{ open ? "Show less" : "Show all" }}
          </button>
        }
      </div>
    }
    @let around = pickerRows();
    @if (around.length) {
      <weave-iteration-picker [loops]="around" (picked)="pick($event)" />
    }
    <div class="sd-pane-body">
      <div class="pane-content" [class.is-editing]="editing()">
        @if (editing()) {
          @defer (on immediate) {
            <weave-output-editor
              #editor
              [session]="session()"
              [initial]="editText()"
              (saved)="saved()"
              (cancelled)="closeEditor()"
            />
          } @placeholder {
            <p class="pane-empty-text">Opening the editor…</p>
          }
        } @else {
          @if (recorded?.handled; as failed) {
            <!-- A run that happened: what failed and how the run went on. -->
            <weave-error-banner [failure]="failed" />
          } @else if (went) {
            <!-- One banner: what happened first, then that the failure is simulated. -->
            <weave-error-banner
              [failure]="went.failure"
              [simulated]="!!failing"
              [overPin]="!!failing && !!pin"
              (remove)="failing && removeFailure(failing.key)"
              (edit)="openSimulate()"
            />
          } @else if (failing) {
            <weave-failure-banner
              [failure]="failing.entry"
              [scope]="failing.scope"
              [leftOut]="failing.leftOut"
              [overPin]="!!pin"
              [sessionOnly]="session().controller.testData.failuresInSession()"
              (remove)="removeFailure(failing.key)"
              (edit)="openSimulate()"
            />
          } @else if (pin) {
            <weave-pinned-banner
              [item]="pickedItem()"
              [forEveryItem]="pin.key === session().target"
              [stale]="pin.stale !== null"
              [reason]="pinFromElsewhere() ? 'elsewhere' : pin.stale"
              [localOnly]="localOnly()"
              [canExecute]="session().controller.canExecute()"
              [executeLabel]="executeLabel()"
              (unpin)="unpin()"
              (keep)="keepPinned()"
              (everyItem)="pinForEveryItem()"
              (execute)="execute()"
            />
          }
          @if (problem()) {
            <p class="pane-problem" role="alert">{{ problem() }}</p>
          }
          @if (realRun(); as running) {
            <div class="pane-empty pane-real-running" role="status">
              <span class="loading-spinner small" aria-hidden="true"></span>
              <h4>
                {{
                  running.checking
                    ? "Checking whether " + running.stepId + " started…"
                    : "Executing " +
                      running.stepId +
                      " in " +
                      running.environment.name +
                      " · " +
                      elapsed(running.since)
                }}
              </h4>
              <button type="button" class="sm" (click)="stopReal()">
                Stop
              </button>
            </div>
          }
          @if (!pin && realWords(); as said) {
            <div
              class="notice pane-run-notice pane-real-notice"
              [attr.data-tone]="said.tone"
              role="status"
            >
              <weave-icon [name]="said.icon" [size]="16" />
              <div>
                <p>{{ said.text }}</p>
                @if (said.failed) {
                  <p>A failed step returns no output.</p>
                }
              </div>
            </div>
          }
          @if (!pin && realFiles().length) {
            <ul class="pane-files" aria-label="Files in this output">
              @for (item of realFiles(); track item.file.id) {
                <li class="pane-file">
                  <weave-icon name="typeFile" [size]="16" />
                  <span class="pane-file-text"
                    >{{ item.label }} · {{ fileDetail(item.file) }}</span
                  >
                  @if (canDownload()) {
                    <button
                      type="button"
                      class="sm"
                      [attr.aria-label]="'Download ' + item.file.filename"
                      (click)="download(item.file)"
                    >
                      <weave-icon name="download" [size]="16" />Download
                    </button>
                  }
                </li>
              }
            </ul>
          }
          @if (runFailedHere(); as said) {
            <div
              class="notice pane-run-notice pane-run-stopped"
              data-tone="danger"
              role="status"
            >
              <weave-icon name="critical" [size]="16" />
              <div>
                <p>{{ said }}</p>
                <p>
                  Execute workflow gave every earlier step the run's data and
                  stopped here.
                </p>
              </div>
            </div>
            @if (stop?.missing) {
              <p class="pane-stopped-text">
                {{ stop!.step }} has no test output, so the simulation can't run
                it. Fix the step, then set a test output or generate a sample to
                go on.
              </p>
              @if (canPin && !pin) {
                <div class="pane-empty-actions">
                  <button type="button" class="sm" (click)="setOutput()">
                    Set test output
                  </button>
                  <button type="button" class="sm" (click)="generate()">
                    <weave-icon name="refresh" [size]="16" />Generate sample
                  </button>
                </div>
              }
            }
          }
          @if (notice(); as said) {
            <div
              class="pane-notice"
              role="status"
              [attr.data-tone]="said.reason === 'callee' ? 'warning' : null"
            >
              <p class="pane-notice-text">
                @if (said.reason === "callee") {
                  <weave-icon name="warning" [size]="16" aria-hidden="true" />
                }
                {{ said.message }}
              </p>
              @if (said.action; as act) {
                <div class="pane-empty-actions">
                  <button type="button" class="sm" (click)="act.run()">
                    {{ act.label }}
                  </button>
                </div>
              }
              @if (said.reason === "callee" && canPin && !pin) {
                <!-- A call without its workflow: test data for the call when
                     Studio can check it (it knows the call's output), or the workflow. -->
                <div class="pane-empty-actions">
                  @if (calleeKnown()) {
                    <button type="button" class="sm" (click)="setOutput()">
                      Set test output
                    </button>
                  }
                  <button type="button" class="sm" (click)="openCallee()">
                    <weave-icon name="external" [size]="16" />Open workflow
                  </button>
                </div>
              } @else if (said.reason === "missing_mock" && canPin && !pin) {
                <div class="pane-empty-actions">
                  <button type="button" class="sm" (click)="setOutput()">
                    Set test output
                  </button>
                  <button type="button" class="sm" (click)="generate()">
                    <weave-icon name="refresh" [size]="16" />Generate sample
                  </button>
                </div>
              }
            </div>
          }
          @if (recorded) {
            @if (recorded.child && callStep()) {
              <!-- The run this call started, first: how it went, and Open run. -->
              @let card = childCard(recorded.child, recorded.runId);
              <div class="pane-child-run" role="group" aria-label="Called run">
                <weave-icon name="callWorkflow" [size]="16" />
                <div class="pane-child-text">
                  <p class="pane-child-title">{{ card.title }}</p>
                  @if (card.line) {
                    <p class="pane-child-line">{{ card.line }}</p>
                  }
                </div>
                <button
                  type="button"
                  class="sm"
                  (click)="openChildRun(recorded.child)"
                >
                  <weave-icon name="external" [size]="16" />Open run
                </button>
              </div>
            }
            @if (recorded.changed) {
              <div class="notice pane-run-notice" data-tone="info">
                <weave-icon name="stale" [size]="16" />
                <p>Changed since this run.</p>
              </div>
            }
            @if (recorded.failed) {
              @let callFailed = callFailure(recorded.runId);
              <div class="notice pane-run-notice" data-tone="danger">
                <weave-icon name="critical" [size]="16" />
                <div>
                  <p>{{ callFailed?.title ?? recorded.failed }}</p>
                  <p>
                    {{
                      callFailed?.detail ?? "A failed step returns no output."
                    }}
                  </p>
                </div>
              </div>
            }
            @switch (recorded.output.kind) {
              @case ("omitted") {
                <div class="pane-omitted">
                  <p class="pane-omitted-top">
                    <weave-icon name="lock" [size]="16" />{{ recorded.omitted }}
                  </p>
                  <p>
                    The platform keeps classified values out of what Studio
                    reads, so the output isn't shown here. Formula previews that
                    read it say so too.
                  </p>
                </div>
              }
              @case ("none") {
                @if (!recorded.failed) {
                  <p class="pane-empty-text">{{ recorded.none }}</p>
                }
              }
              @case ("too-large") {
                <p class="pane-empty-text">This output is too large to show.</p>
              }
              @case ("loading") {
                <p class="sr-only" role="status">Reading the step's output…</p>
                <span class="pane-skeleton" aria-hidden="true"></span>
                <span class="pane-skeleton short" aria-hidden="true"></span>
              }
              @case ("error") {
                <div class="pane-notice" role="status">
                  <p class="pane-notice-text">{{ recorded.error }}</p>
                  <button type="button" class="sm" (click)="retryRun()">
                    Try again
                  </button>
                </div>
              }
            }
            @if (!callStep() && recorded.child; as child) {
              <p class="pane-called-run">
                Called run {{ child.slice(0, 8) }}
                <button type="button" class="sm" (click)="openRun(child)">
                  <weave-icon name="external" [size]="16" />Open in Operate
                </button>
              </p>
            }
          }
          @let handling = went?.failure?.policy ?? recorded?.handled?.policy;
          @if (handling === "errorOutput") {
            <!-- What later steps read: the error path's result. -->
            @let from = resultFrom();
            <p class="pane-result-caption">
              Result on error{{ from ? " · from " + from : "" }}
            </p>
          }
          @if (loadingRun(); as run) {
            <p class="pane-empty-text pane-loading-run" role="status">
              <span class="loading-spinner small" aria-hidden="true"></span
              >Loading run {{ run }}…
            </p>
          } @else if (noRunShown(); as why) {
            <p class="pane-empty-text">{{ why }}</p>
          } @else if (recorded && recorded.output.kind !== "value") {
            <!-- What the run recorded says it all. -->
          } @else if (none) {
            <p class="pane-empty-text">{{ none }}</p>
          } @else if (returnedNull() && handling === "continue") {
            <p class="pane-result-caption">Output</p>
            <pre class="pane-json" tabindex="0" aria-label="Output data">
null</pre
            >
            <p class="pane-empty-text pane-result-note">
              Later steps read null here and can read
              {{ session().target }}'s error.
            </p>
          } @else if (returnedNull()) {
            <p class="pane-empty-text">This step returned no data (null).</p>
          } @else if (event() !== undefined && view() !== "schema") {
            @if (view() === "table") {
              <!-- A loop's results: the item each came from first. -->
              @let loop = loopTable();
              <weave-data-table
                [value]="loop ? loop.value : event()"
                label="Output data"
                [query]="query()"
              />
              @if (loop?.note; as note) {
                <p class="data-table-note">{{ note }}</p>
              }
            } @else {
              <pre class="pane-json" tabindex="0" aria-label="Output data">{{
                json(event())
              }}</pre>
            }
          } @else if (view() === "schema" && paths) {
            <weave-path-list [rows]="paths.rows" [result]="paths.result" />
          } @else if (view() === "schema") {
            @if (answerable()) {
              <weave-script-editor [session]="session()" />
            }
            @if (isTrigger() && event() === undefined) {
              <p class="pane-empty-text">No test event yet</p>
            }
            @if (!isTrigger() && event() === undefined) {
              @if (session().controller.running(); as runs) {
                <div class="pane-empty" role="status">
                  <span class="loading-spinner small" aria-hidden="true"></span>
                  <h4>Executing…</h4>
                </div>
              } @else if (!pin && realEnded()) {
                <!-- A real run ended without output: the notice above says
                     why; one row offers the next try. -->
                <div class="pane-empty-actions pane-real-actions">
                  @if (session().controller.canExecute()) {
                    <button type="button" class="sm" (click)="execute()">
                      <weave-icon name="execute" [size]="16" />{{
                        executeLabel()
                      }}
                    </button>
                  }
                  @if (canPin) {
                    <button type="button" class="sm" (click)="setOutput()">
                      Set test output
                    </button>
                  }
                </div>
              } @else if (!notice() && !stop && !realRun()) {
                <!-- While a step test runs, the running block above stands in for it. -->
                <div class="pane-empty">
                  <span class="pane-empty-icon" aria-hidden="true"
                    ><weave-icon name="execute" [size]="20"
                  /></span>
                  <h4>No output yet</h4>
                  <p>
                    {{
                      callStep()
                        ? "Execute the workflow to see this step's output."
                        : "Execute this step to see its output."
                    }}{{
                      canPin
                        ? " You can also set test output, or generate a sample."
                        : ""
                    }}
                  </p>
                  <div class="pane-empty-actions">
                    @if (session().controller.canExecute()) {
                      <button type="button" class="sm" (click)="execute()">
                        <weave-icon name="execute" [size]="16" />{{
                          executeLabel()
                        }}
                      </button>
                    }
                    @if (canPin) {
                      <button type="button" class="sm" (click)="setOutput()">
                        Set test output
                      </button>
                      <button type="button" class="sm" (click)="generate()">
                        <weave-icon name="refresh" [size]="16" />Generate sample
                      </button>
                    }
                  </div>
                </div>
              }
            }
            @if (detachedCall()) {
              <!-- A call that didn't wait has no result to describe. -->
            } @else if (rows.length) {
              <weave-data-tree [rows]="shown" label="Output fields" />
            } @else if (!stop) {
              <p class="pane-empty-text">
                This step's output can be any value.
              </p>
            }
          } @else {
            <div class="pane-empty">
              <span class="pane-empty-icon" aria-hidden="true"
                ><weave-icon name="execute" [size]="20"
              /></span>
              <h4>
                {{ isTrigger() ? "No test event yet" : "No output yet" }}
              </h4>
              <p>
                Schema shows the fields later steps can read. Executing this
                step isn’t available yet.
              </p>
            </div>
          }
        }
        @if (recorded) {
          <weave-run-step-logs
            [runId]="recorded.runId"
            [stepId]="session().target"
            [iterationKey]="recorded.iteration"
            [api]="session().host.api"
            [scope]="recorded.scope"
            (refused)="session().controller.run()?.refused()"
          />
        }
      </div>
    </div>`,
})
export class OutputPane {
  session = input.required<FormSession>();
  /** What the step returned, once test data or a simulation provides it. */
  value = input<Json | undefined>(undefined);
  readonly chosenView = signal<DataView | undefined>(savedView("output"));
  view(): DataView {
    // A decision that ran opens on its paths (Schema).
    return (
      this.chosenView() ??
      (this.paths() ? "schema" : defaultOutputView(this.event()))
    );
  }
  readonly searching = signal(false);
  readonly query = signal("");
  readonly countLabel = countLabel;
  readonly sampleCount = sampleCount;
  readonly tableCount = tableCount;
  readonly views: { id: DataView; label: string; icon: string }[] = [
    { id: "schema", label: "Schema", icon: "schemaView" },
    { id: "table", label: "Table", icon: "tableView" },
    { id: "json", label: "JSON", icon: "jsonView" },
  ];
  readonly radioKey = radioKey;
  private readonly lifetime = inject(DestroyRef);
  constructor() {
    // Another step opened in step details: an open edit belonged to the previous one.
    effect(() => {
      this.session();
      untracked(() => this.editing.set(false));
    });
  }
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  /** Ticks every second while a step test runs here, for its elapsed time. */
  private readonly clock = signal(Date.now());
  private readonly ticking = effect((onCleanup) => {
    if (!this.realRun()) return;
    const timer = setInterval(() => this.clock.set(Date.now()), 1000);
    onCleanup(() => clearInterval(timer));
  });
  readonly short = shortId;
  /** This step's step test running in the environment now, if any. */
  realRun(): RunningStepTest | null {
    const { host, target } = this.session();
    const running = connectionsPort(host)?.runningTests()?.running() ?? null;
    return running?.stepId === target ? running : null;
  }
  elapsed(since: number): string {
    const seconds = Math.max(0, Math.round((this.clock() - since) / 1000));
    return seconds < 60
      ? `${seconds} s`
      : `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
  }
  stopReal() {
    const { host } = this.session();
    void connectionsPort(host)?.runningTests()?.stop();
  }
  /** Where this step's last result ran for real, when it did. */
  realOf(): RealResult | null {
    const { controller, target } = this.session();
    return controller.realResult(target);
  }
  /** Why a real result shows no output, or how it ended when it didn't succeed. */
  /** A real run of this step ended without output: failed, timed out or canceled. */
  realEnded(): boolean {
    const status = this.realOf()?.status;
    return (
      status === "failed" || status === "timed_out" || status === "cancelled"
    );
  }
  realWords(): {
    text: string;
    tone: string;
    icon: string;
    /** Failed or timed out: no output comes back, as the Runs tab says. */
    failed?: boolean;
  } | null {
    const real = this.realOf();
    if (!real) return null;
    if (real.omitted)
      return { text: omissionWords(real.omitted), tone: "info", icon: "lock" };
    const text = realEndWords(
      this.session().target,
      real.environmentName,
      { status: real.status, error: real.error ?? null },
      real.timeoutSeconds ?? 0,
    );
    return text
      ? {
          text,
          tone: real.status === "cancelled" ? "info" : "danger",
          icon: real.status === "cancelled" ? "info" : "critical",
          failed: real.status === "failed" || real.status === "timed_out",
        }
      : null;
  }
  /** A real result that can become test data: it succeeded and holds an output. */
  realPinnable(): boolean {
    const { controller, target } = this.session();
    const real = this.realOf();
    return (
      !!real &&
      real.status === "succeeded" &&
      !this.pinned() &&
      controller.pickedStepData(target)?.source === "test-call" &&
      !controller.readOnlyReason(target)
    );
  }
  async pinReal() {
    const { controller, target } = this.session();
    const refused = await controller.pinReal(target);
    this.problem.set(refused ?? "");
    if (!refused) this.focusPin();
  }
  /** Files a real result returned, each with its name, size and type. */
  realFiles(): { label: string; file: FileReference }[] {
    const { controller, target } = this.session();
    if (!this.realOf()) return [];
    const data = controller.pickedStepData(target);
    return data?.source === "test-call" ? filesIn(data.value) : [];
  }
  fileDetail(file: FileReference): string {
    return fileValue(file, "real")?.detail ?? file.filename;
  }
  canDownload(): boolean {
    return this.session().host.can("file.read");
  }
  async download(file: FileReference) {
    const { host, target } = this.session();
    const alive = () => !this.lifetime.destroyed;
    try {
      const result = await downloadFile(
        {
          api: host.api,
          key: target,
          canRead: host.can("file.read"),
          canManage: false,
          active: alive,
        },
        file,
        alive,
      );
      if (!alive()) return;
      const url = URL.createObjectURL(result.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = result.file.filename;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (alive())
        this.problem.set(
          `Studio couldn't download ${file.filename}: ${describeError(error).message}`,
        );
    }
  }

  noOutput(): string | null {
    const step = this.session().controller.step(this.session().target);
    return step ? noOutputSentence(step) : null;
  }
  /** The input form or a trigger choice: its output is the test event. */
  isTrigger(): boolean {
    return isTriggerTarget(this.session().target);
  }
  /** The step's data: its output once known, its pin or its last result, and the trigger's test event. */
  /**
   * A loop's results as its Table shows them: each with the item it came
   * from ("Item"), and a note naming the items Skip empty results left out;
   * null for another step, or when Studio can't tell which item gave which.
   */
  /** The Table's count: a loop's results, as its Item column shows them, and the items it left out. */
  tableCountOf(): string {
    const loop = this.loopTable();
    return loop
      ? tableCount(loop.value, this.query()) + this.skippedText()
      : tableCount(this.event(), this.query());
  }
  /** " · 2 skipped": the items Skip empty results left out of a loop's results; "" otherwise. */
  skippedText(): string {
    const left = this.loopTable()?.left ?? 0;
    return left ? ` · ${left.toLocaleString("en-US")} skipped` : "";
  }
  loopTable(): { value: Json; note: string | null; left: number } | null {
    const { controller, target } = this.session();
    if (controller.viewingRuns() || controller.step(target)?.kind !== "forEach")
      return null;
    const results = this.event();
    const placed = controller.loopResults(target);
    if (!Array.isArray(results) || !placed) return null;
    return {
      value: results.map((result, at) => ({
        Item: placed.items[at],
        ...(result !== null &&
        typeof result === "object" &&
        !Array.isArray(result)
          ? result
          : { Value: result }),
      })),
      note: leftOutText(placed.left),
      left: placed.left.length,
    };
  }
  event(): Json | undefined {
    if (this.value() !== undefined) return this.value();
    const { controller, target } = this.session();
    return isTriggerTarget(target)
      ? controller.testEvent()
      : controller.pickedStepData(target)?.value;
  }
  /**
   * A decision that ran: its paths as the last execution took them, the
   * count's words ("Took path 1") and the taken path's result; null otherwise.
   */
  paths(): { rows: PathRow[]; took: string; result: Json | undefined } | null {
    const { controller, target, host } = this.session();
    // The last local execution's path isn't the run's.
    if (controller.viewingRuns()) return null;
    const step = controller.step(target);
    if (step?.kind !== "switch" || this.pinned()) return null;
    // The path it took, as kept with its last result (also after a reload).
    const variables = branchVariables(
      host.execution?.results() ?? null,
      target,
    );
    const rows = pathRows(step, variables, host.model.definition);
    const taken = rows?.find((row) => row.outcome === "taken");
    if (!rows || !taken) return null;
    const branch = Object.values(variables?.branches ?? {})[0] as
      | { output?: Json }
      | undefined;
    return {
      rows,
      took:
        taken.label === "Otherwise"
          ? "Took Otherwise"
          : `Took ${taken.label.toLowerCase()}`,
      result: branch?.output,
    };
  }
  /** Signals and human tasks take a test answer. */
  answerable(): boolean {
    const { controller, target } = this.session();
    // A run's answers happened: nothing to script here.
    if (controller.run()) return false;
    const kind = controller.step(target)?.kind as string | undefined;
    return kind === "signal" || kind === "humanTask";
  }
  /** The step ran and returned null. */
  returnedNull(): boolean {
    const { controller, target } = this.session();
    if (target.startsWith("$")) return false;
    const data = controller.pickedStepData(target);
    // A decision whose path has no result says so in its path list.
    return (
      (data?.source === "simulated" || data?.source === "run") &&
      data.value === null &&
      !this.paths()
    );
  }
  /**
   * In run mode: what the step returned in the run (or why not), its tags,
   * its failure and whether it changed since; null outside run mode.
   */
  runView() {
    const { controller, target, host } = this.session();
    const run = controller.run();
    if (!run || target.startsWith("$")) return null;
    const output = controller.runOutput(target);
    const key = controller.runKey(target);
    const several = run.iterations(target).length > 1;
    const state = run.state(key);
    const code = state === "failed" ? run.failure(target) : null;
    // It failed and its On error went on: the banner says how, over its result.
    const handled = state === "handled" ? run.handled(key) : null;
    const canvas = host.canvasRun?.() ?? null;
    const item = /\[(\d+)\]$/.exec(key);
    return {
      output,
      runId: run.runId,
      iteration: several ? key : null,
      scope: `${host.stepDataScope ?? ""}|${host.profile?.environmentId ?? ""}`,
      tag: `From ${run.label()}${several && item ? `, item ${Number(item[1]) + 1}` : ""}`,
      handled,
      pill: handled
        ? "Failed, continued"
        : state === "failed"
          ? "Failed in this run"
          : output.kind === "omitted"
            ? "Not shown"
            : state === "running"
              ? "Running in this run"
              : state === "not-reached"
                ? "Not reached"
                : "Finished in this run",
      tone: handled
        ? "warning"
        : state === "failed"
          ? "danger"
          : output.kind === "omitted" || state === "not-reached"
            ? null
            : "success",
      failed: code ? `This step failed in ${run.label()}: ${code}.` : null,
      changed: isRecordedRun(canvas) && canvas.changed.includes(target),
      child: run.childRun(key),
      omitted:
        output.kind === "omitted"
          ? (OMISSION_WORDS[output.reason] ?? "This output can't be shown.")
          : "",
      none:
        output.kind === "none"
          ? output.why === "not-reached"
            ? "This run didn't reach this step."
            : output.why === "running"
              ? "This step hasn't finished in this run yet."
              : "The platform didn't record this step's output."
          : "",
      error: output.kind === "error" ? output.message : "",
    };
  }
  retryRun() {
    const { controller, target } = this.session();
    controller.retryRunOutput(target);
  }
  openRun(id: string) {
    void this.session().host.viewRun?.({ id });
  }
  /** The runs this window read the summary of, by account and run ID; a run asked once isn't asked again. */
  private readonly childSummaries = signal<ReadonlyMap<string, RunSummary>>(
    new Map(),
  );
  private readonly askedChildren = new Set<string>();
  /** A call step's child run on the Runs tab: what started it, and how it went once its summary arrives. */
  childCard(child: string, parent: string) {
    const { controller, target, host } = this.session();
    const step = controller.step(target);
    const uses = typeof step?.["uses"] === "string" ? step["uses"] : "";
    const name = uses.slice(0, Math.max(0, uses.lastIndexOf("@"))) || uses;
    const detached = step?.["mode"] === "detach";
    const key = `${host.stepDataScope ?? ""}|${child}`;
    const summary = this.childSummaries().get(key);
    if (!summary && !this.askedChildren.has(key)) {
      this.askedChildren.add(key);
      // The parent's child runs hold this one's summary: its version, status and time.
      void childSummary(asRunViewsApi(host.api), parent, child)
        .then((found) => {
          if (found)
            this.childSummaries.update((kept) => new Map(kept).set(key, found));
        })
        .catch(() => undefined);
    }
    return {
      title: `${detached ? "Started" : "Ran as"} run ${shortId(child)} of ${name}`,
      // A call that didn't wait says so, then how the run it started went, once read.
      line: detached
        ? [
            `${host.model.definition.metadata.name} didn't wait for it`,
            ...(summary ? [childLine(summary)] : []),
          ].join(" · ")
        : summary
          ? childLine(summary)
          : "",
    };
  }
  /** A call step that failed in the run: its call failed, and If it fails stopped the workflow. */
  callFailure(runId: string) {
    if (!this.callStep()) return null;
    const { target, host } = this.session();
    return {
      title: `${target} failed in run ${shortId(runId)}: its call failed.`,
      detail: `If it fails is Stop this workflow, so ${host.model.definition.metadata.name} stopped here. Open the run to see the step that failed.`,
    };
  }
  /** Open run: the called workflow's Runs tab on that run, as Debug in editor opens a run. */
  async openChildRun(id: string) {
    const session = this.session();
    const host = session.host;
    if (!host.restoreWorkflow || !host.navigate || !host.selectTab)
      return this.openRun(id);
    const { openRunInEditor } = await import("../../runs-tab/open-run");
    await openRunInEditor(
      {
        api: host.api,
        profile: host.profile,
        stepDataScope: host.stepDataScope ?? "",
        get draftId() {
          return host.draftId ?? "";
        },
        get draftRevision() {
          return host.draftRevision;
        },
        get model() {
          return host.model;
        },
        dialogs: { confirm: (options) => session.confirm(options) },
        notify: (text, action, tone) => host.notify(text, action, tone),
        restoreWorkflow: (draft) => host.restoreWorkflow!(draft),
        navigate: (view) => host.navigate!(view),
        selectTab: (tab) => host.selectTab!(tab),
      },
      id,
      null,
    );
  }
  /** A call that doesn't wait: it has no result. */
  detachedCall(): boolean {
    const { controller, target } = this.session();
    return this.callStep() && controller.step(target)?.["mode"] === "detach";
  }
  /** A call that finished in the run while the run it called failed. */
  calledRunFailed(recorded: { child: string | null; pill: string }): boolean {
    // A call that doesn't wait finished on its own, whatever its run did.
    if (!recorded.child || !this.callStep() || this.detachedCall())
      return false;
    const key = `${this.session().host.stepDataScope ?? ""}|${recorded.child}`;
    return (
      recorded.pill === "Finished in this run" &&
      this.childSummaries().get(key)?.status === "failed"
    );
  }
  /** Studio knows what the called workflow returns, so test output for the call can be checked. */
  calleeKnown(): boolean {
    const { controller, target } = this.session();
    const uses = controller.step(target)?.["uses"];
    return (
      typeof uses === "string" &&
      controller.kindContext().workflowContract(uses) !== null
    );
  }
  /** Open workflow, from a call that needs the workflow it calls. */
  openCallee() {
    const { controller, target, host } = this.session();
    const uses = controller.step(target)?.["uses"];
    if (typeof uses === "string" && uses) host.openWorkflow?.(uses);
  }
  /** A step that calls a workflow: it runs only when the whole workflow executes. */
  callStep(): boolean {
    const { controller, target } = this.session();
    return (controller.step(target)?.kind as string) === "callWorkflow";
  }
  executeLabel(): string {
    return this.callStep() ? "Execute workflow" : "Execute step";
  }
  /** Execute step from the pane (its empty state, the stale pin's banner). */
  execute() {
    this.session().controller.requestExecute("step");
  }
  /** An AI task's model requests and tokens for the output shown ("1 request · 812 in · 64 out tokens"). */
  usage(): string | null {
    return this.aiTask() ? usageLine(this.event()) : null;
  }
  // ------------------------------------------------- an AI task's answer
  /** The step is an AI task (its test data is the answer Execute step replays). */
  aiTask(): boolean {
    const { controller, target } = this.session();
    return controller.descriptor(target)?.script === "ai-turns";
  }
  /** The answer shown is the one kept as test data: Execute step replays it. */
  answerKept(): boolean {
    const { controller, target } = this.session();
    return !controller.viewingRuns() && controller.keptAnswer(target);
  }
  /** A text answer to read in full above its fields; null for structured results. */
  answerText(): string | null {
    const shown = this.event();
    const result =
      shown && typeof shown === "object" && !Array.isArray(shown)
        ? (shown as Record<string, Json>)["result"]
        : undefined;
    return this.aiTask() && typeof result === "string" && result
      ? result
      : null;
  }
  readonly answerId = `pane-answer-${++answerSequence}`;
  /** The answer shown in full (Show all), until another answer shows. */
  readonly answerOpen = signal<string | null>(null);
  /** The clamped answer has more lines than it shows. */
  readonly answerLonger = signal(false);
  private readonly answerElement =
    viewChild<ElementRef<HTMLElement>>("answerText");
  private readonly measureAnswer = afterEveryRender({
    read: () => {
      const element = this.answerElement()?.nativeElement;
      const longer =
        !!element &&
        (this.answerOpen() === this.answerText() ||
          element.scrollHeight > element.clientHeight + 1);
      if (longer !== untracked(this.answerLonger))
        this.answerLonger.set(longer);
    },
  });
  toggleAnswer(text: string) {
    this.answerOpen.set(this.answerOpen() === text ? null : text);
  }
  /**
   * After an execution in which this step (the item picked, in a loop)
   * failed and its On error went on: the failure, how long ago, and the
   * step whose output is the error path's result; null otherwise.
   */
  handledRun() {
    const { controller, target } = this.session();
    if (target.startsWith("$")) return null;
    const found = controller.handledResult(target);
    if (!found) return null;
    return { failure: found.failure, when: relativeTime(found.at) };
  }
  /** The step whose output is the error path's result ("flag-request"); null when it reads none. */
  resultFrom(): string | null {
    const { controller, target } = this.session();
    // A run shows the result its own version took, never the draft's.
    const recorded = this.runView();
    if (recorded) return recorded.handled?.from ?? null;
    return errorResultSource(controller.step(target));
  }
  /** What the step returned in the last execution, when no pin stands for it. */
  simulated(): boolean {
    const { controller, target } = this.session();
    return (
      !target.startsWith("$") &&
      controller.pickedStepData(target)?.source === "simulated"
    );
  }
  /** What the last execution said about this step (it needs test data, a path it didn't take…). */
  notice() {
    const { controller, target } = this.session();
    if (controller.viewingRuns()) return null;
    const notice = controller.executeNotice();
    return notice?.target === target ? notice : null;
  }
  readonly noPinYet = "Set test output or generate a sample to pin it.";
  readonly problem = signal("");
  pinned() {
    const { controller, target } = this.session();
    // The draft's pin isn't what the run returned.
    if (controller.viewingRuns()) return null;
    if (target.startsWith("$")) return null;
    // Inside a loop's body: the item's own pin, else the one for every item.
    return (
      controller.pinOfTarget(controller.pinKey(target)) ??
      controller.pinOfTarget(target)
    );
  }
  /** The same picker row as the Input pane's: both show the same item. None outside loops or on the Runs tab. */
  pickerRows(): PickerLoop[] {
    const { controller, target } = this.session();
    if (controller.viewingRuns() || target.startsWith("$")) return [];
    return loopRows(controller.iterations, target, (id) => controller.step(id));
  }
  pick({ loopId, index }: { loopId: string; index: number }) {
    this.session().controller.iterations.pick(loopId, index);
  }
  /** The item picked inside a loop's body, one-based and as people count; null elsewhere. */
  pickedItem(): string | null {
    const { controller, target } = this.session();
    const key = controller.pinKey(target);
    if (key === target) return null;
    return [...key.matchAll(/\[(\d+)\]/g)]
      .map((match) => (Number(match[1]) + 1).toLocaleString("en-US"))
      .join(" › ");
  }
  /** "Pinned", or inside a loop "Pinned for item 3" / "Pinned for every item". */
  pinWords(key: string): string {
    const item = this.pickedItem();
    if (item === null) return "Pinned";
    return key === this.session().target
      ? "Pinned for every item"
      : `Pinned for item ${item}`;
  }
  /** "Simulated", or "Simulated · item 3" inside a loop's body. */
  simulatedWords(): string {
    const item = this.pickedItem();
    return item === null ? "Simulated" : `Simulated · item ${item}`;
  }
  /** On the Runs tab with no run's data: why Output shows nothing; null otherwise. */
  noRunShown(): string | null {
    const { controller, host } = this.session();
    if (!controller.viewingRuns() || controller.run()) return null;
    if (this.loadingRun()) return null;
    return host.runView?.()?.listed
      ? "Choose a run to see what this step returned."
      : "There's no run to show here.";
  }
  /** After Debug in editor, on the step where Execute stopped as the run failed: the step, and whether it wants test data. */
  stoppedHere(): { step: string; missing: boolean } | null {
    const { host, target } = this.session();
    const notice = host.runNotice;
    if (!notice || notice.draftId !== host.draftId) return null;
    if (instanceStep(notice.key) !== instanceStep(target)) return null;
    return { step: instanceStep(notice.key), missing: notice.missing };
  }
  /** After Debug in editor, on the step where the run failed: its error. */
  runFailedHere(): string | null {
    const { host, target } = this.session();
    const notice = host.runNotice;
    if (!notice || notice.draftId !== host.draftId) return null;
    if (instanceStep(notice.key) !== instanceStep(target)) return null;
    return `In run ${shortId(notice.runId)}, this step failed${notice.code ? `: ${notice.code}` : ""}.`;
  }
  /** The chosen run's short ID while it loads; null otherwise. */
  loadingRun(): string | null {
    const { controller, host } = this.session();
    const shown = host.runView?.();
    return controller.viewingRuns() &&
      !controller.run() &&
      shown?.loading &&
      shown.chosen
      ? shortId(shown.chosen)
      : null;
  }
  pinnable(): boolean {
    const { controller, target } = this.session();
    return !target.startsWith("$") && controller.pinnable(target);
  }
  /** The pin came from another workflow and isn't kept here yet. */
  pinFromElsewhere(): boolean {
    const { controller, target } = this.session();
    return controller.pinFromElsewhere(target);
  }
  localOnly(): boolean {
    const { controller, target } = this.session();
    const key = (this.pinned()?.key ?? target)
      .replace(/~/g, "~0")
      .replace(/\//g, "~1");
    return controller.testData.localOnly().has(`/pins/${key}`);
  }
  pinTitle(pinned: boolean): string {
    if (pinned) return "Pin output (P)";
    if (this.answerKept())
      return "Kept as test data. Execute step replays this answer.";
    if (this.aiTask() && this.realPinnable())
      return "Keep as test data: Execute step replays this answer.";
    return this.realPinnable()
      ? "Pin output: keep this run's output as test data"
      : this.noPinYet;
  }
  togglePin() {
    if (!this.pinned()) {
      // A second click on a kept answer removes it, with Undo.
      if (this.answerKept()) return void this.forgetAnswer();
      if (this.realPinnable()) return void this.pinReal();
      this.problem.set(this.noPinYet);
      return;
    }
    this.unpin();
  }
  /**
   * The failure simulated for this step, or the item picked: the item's
   * own first, else every item's. None on the Runs tab.
   */
  failure(): {
    key: string;
    entry: ErrorEntry;
    scope: "step" | "item" | "every";
    /** Why Execute leaves it out under the step's current setting; null when it fails the step. */
    leftOut: string | null;
  } | null {
    const { controller, target } = this.session();
    if (controller.viewingRuns() || target.startsWith("$")) return null;
    const data = controller.testData;
    data.version();
    const key = controller.pinKey(target);
    const found = failureOf(data.data(), key);
    if (!found) return null;
    const scope =
      key === target ? "step" : found.key === target ? "every" : "item";
    // The step changed since (its On error back to Stop the run, a call
    // that no longer waits): the failure stays, and Execute doesn't send it.
    const step = controller.step(target);
    const block = step ? simulateBlock(step, false) : undefined;
    const leftOut =
      block === null
        ? null
        : `Execute leaves this failure out. ${block ?? "This step can't fail in a simulation."}`;
    // A transform's or a decision table's failure didn't start, whatever
    // outcome the test data holds: the platform reports it so.
    const fixed = step ? simulatedOutcome(step) : null;
    const entry = fixed ? { ...found.entry, outcome: fixed } : found.entry;
    return { ...found, entry, scope, leftOut };
  }
  /**
   * Inside an error path whose step won't fail with this test data: that
   * step ("submit"), whose failure to simulate first; null otherwise.
   */
  untakenOwner(): string | null {
    const { controller, target, host } = this.session();
    if (controller.viewingRuns() || target.startsWith("$")) return null;
    const data = controller.testData;
    data.version();
    return (
      untakenPath(
        host.model.definition.spec.steps ?? [],
        target,
        (owner) => !!failureOf(data.data(), controller.pinKey(owner)),
        (owner) => simulateBlock(owner, false) === null,
      )?.owner ?? null
    );
  }
  /** The "⋯" menu after Pin and Edit: Simulate a failure…, where it applies. */
  moreActions(): RowMenuItem[] | null {
    const { controller, target } = this.session();
    const step = controller.step(target);
    if (!step || controller.viewingRuns() || target.startsWith("$"))
      return null;
    // Step details open this workflow's own steps, never a called one's.
    const reason = simulateBlock(step, false);
    if (reason === undefined) return null;
    return [
      {
        label: "Simulate a failure…",
        detail: reason ?? undefined,
        disabled: reason !== null,
        run: () => this.openSimulate(),
      },
    ];
  }
  /** Open while the dialog is: what it was opened for, kept as it was then. */
  readonly simulating = signal<{
    key: string;
    item: string | null;
    loop: string;
    changesData: boolean;
    outcome: "not_started" | null;
    suggestions: ReturnType<typeof codeSuggestions>;
    initial: FailureChoice | null;
    /** The failure being edited, to replace when Which items changes. */
    editing: string | null;
  } | null>(null);
  openSimulate() {
    const { controller, target } = this.session();
    const opening = simulateOpening(controller, target);
    if (!opening) return;
    const current = this.failure();
    this.simulating.set({
      ...opening,
      initial: current && {
        code: current.entry.code,
        outcome: current.entry.outcome ?? "failed",
        every: current.scope === "every",
      },
      editing: current?.key ?? null,
    });
  }
  async simulate(choice: FailureChoice) {
    const open = this.simulating();
    if (!open) return;
    const { controller, target } = this.session();
    // The answer is for the item picked when the dialog opened.
    if (controller.pinKey(target) !== open.key) {
      this.problem.set(
        "The picked item changed while the dialog was open, so nothing was simulated.",
      );
      return;
    }
    const key = failureKey(open.key, choice.every);
    // "failed" is the platform's default: it isn't written.
    const written = await controller.setFailure(key, {
      code: choice.code,
      ...(choice.outcome !== "failed" ? { outcome: choice.outcome } : {}),
    });
    if (!written.ok) return this.problem.set(written.message);
    this.problem.set("");
    if (open.editing && open.editing !== key)
      await controller.setFailure(open.editing, null);
  }
  closeSimulate() {
    this.simulating.set(null);
    // Back to the menu that opened it (its item is gone with the menu).
    this.element.nativeElement
      .querySelector<HTMLElement>(".pane-more .row-menu-toggle")
      ?.focus();
  }
  async removeFailure(key: string) {
    const removed = await this.session().controller.setFailure(key, null);
    this.problem.set(removed.ok ? "" : removed.message);
  }
  unpin() {
    this.problem.set("");
    const { controller, target } = this.session();
    controller.unpin(this.pinned()?.key ?? target);
    this.focusPin();
  }
  /** Removes the AI task's kept answer (the Undo toast says so). */
  async forgetAnswer() {
    this.problem.set("");
    const { controller, target } = this.session();
    await controller.forgetAnswer(target);
    this.focusPin();
  }
  /** The item's pin, for every item: pinned on the step, and the item's own goes. */
  async pinForEveryItem() {
    const { controller, target } = this.session();
    const pin = this.pinned();
    if (!pin || pin.key === target) return;
    const refused = await controller.pinForEveryItem(pin.key);
    this.problem.set(refused ?? "");
    if (refused) return;
    this.focusPin();
  }
  keepPinned() {
    const { controller, target } = this.session();
    controller.keepPinned(this.pinned()?.key ?? target);
    this.focusPin();
  }
  /**
   * The control that started it may go (Generate sample once pinned, the
   * banner's links as it changes): focus stays in step details, on the pin
   * toggle, which stays in place.
   */
  private focusPin() {
    setTimeout(() => {
      if (this.lifetime.destroyed) return;
      this.element.nativeElement
        .querySelector<HTMLElement>(".pane-pin")
        ?.focus({ preventScroll: true });
    });
  }
  async generate() {
    const session = this.session();
    let refused: string | null;
    try {
      refused = await session.controller.generateOutput(session.target);
    } catch {
      // Never silent: a sample that couldn't be made or kept says so.
      refused = CANT_SAMPLE;
    }
    if (this.session() !== session) return;
    this.problem.set(refused ?? "");
    if (!refused) {
      // An AI task keeps a sample answer; other steps pin a sample output.
      session.announce(
        this.aiTask()
          ? `Kept a sample answer as test data for ${session.target}.`
          : `Pinned a sample output for ${session.target}.`,
      );
      this.focusPin();
    }
  }
  /** Set test output and Edit output: the JSON editor, with the pinned value or {}. */
  setOutput() {
    if (this.editing()) return this.editor()?.focus();
    const pinned = this.event();
    this.editText.set(
      pinned === undefined ? "{}" : JSON.stringify(pinned, null, 2),
    );
    this.problem.set("");
    this.editing.set(true);
  }
  readonly editing = signal(false);
  readonly editText = signal("{}");
  /**
   * The open editor, queried by name: naming its class here would load it,
   * and CodeMirror with it, with step details instead of on first use.
   */
  private readonly editor = viewChild<{
    dirty(): boolean;
    save(): Promise<boolean>;
    focus(): void;
  }>("editor");
  /** An output edit not saved yet: closing asks first. */
  editDirty(): boolean {
    return this.editing() && !!this.editor()?.dirty();
  }
  /** Saves the open edit (the guard's Save); true once pinned. */
  async saveEdit(): Promise<boolean> {
    const editor = this.editor();
    return editor ? editor.save() : true;
  }
  focusEditor() {
    this.editor()?.focus();
  }
  saved() {
    const session = this.session();
    this.editing.set(false);
    session.announce(`Pinned the output of ${session.target}.`);
    this.focusEdit();
  }
  closeEditor() {
    this.editing.set(false);
    this.focusEdit();
  }
  private focusEdit() {
    setTimeout(() =>
      this.element.nativeElement
        .querySelector<HTMLElement>(".pane-edit")
        ?.focus({ preventScroll: true }),
    );
  }
  rows() {
    const session = this.session();
    const target = session.target;
    if (isTriggerTarget(target))
      return schemaRows(session.host.model.definition.spec["inputSchema"]);
    if (target === "$end")
      return schemaRows(session.host.model.definition.spec["outputSchema"]);
    // After its error path ran, the step's output is the path's result: the
    // fields of the step that result reads, never the step's own; in a run
    // that happened as in one Studio executed.
    const policy =
      this.handledRun()?.failure.policy ?? this.runView()?.handled?.policy;
    const from = policy === "errorOutput" ? this.resultFrom() : null;
    const step =
      policy === "errorOutput"
        ? from
          ? session.controller.step(from)
          : null
        : session.controller.step(target);
    const schema = step
      ? ndvRegistry
          .kind(step.kind)
          ?.outputSchema(step, session.controller.kindContext())
      : null;
    // With pinned output, each field shows its value.
    return withSamples(schemaRows(schema), this.event(), "");
  }
  filter(rows: ReturnType<OutputPane["rows"]>) {
    return filterRows(rows, this.query());
  }
  json(value: Json | undefined): string {
    return JSON.stringify(value, null, 2);
  }
  setView(view: DataView) {
    this.chosenView.set(view);
    writeView("output", view);
  }
  toggleSearch() {
    if (this.searching()) return this.closeSearch();
    this.focusSearch();
  }
  focusSearch() {
    this.searching.set(true);
    const session = this.session();
    setTimeout(() => {
      if (
        !this.lifetime.destroyed &&
        this.session() === session &&
        session.isCurrent() &&
        this.element.nativeElement.getClientRects().length
      )
        this.element.nativeElement
          .querySelector<HTMLInputElement>(".pane-search")
          ?.focus();
    });
  }
  closeSearch(event?: Event) {
    event?.preventDefault();
    event?.stopPropagation();
    this.searching.set(false);
    this.query.set("");
    this.element.nativeElement
      .querySelector<HTMLElement>(".pane-search-toggle")
      ?.focus();
  }
}
