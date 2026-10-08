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
// The list pages (Workflows, Runs, My tasks, Email, Connections):
// heading, filters, a table with per-view columns and the detail panel. The
// shell (App) owns the state and the commands; this view renders them and
// loads lazily (@defer), outside the initial bundle.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import type { App, View } from "../app";
import { Icon } from "../icon";
import { ModalSheet, sheetWhen } from "../modal-sheet";
import { TaskForm } from "../task-form";
import { FilePicker } from "../forms/ui/file-picker";
import { filesIn } from "../forms/core/file-reference";
import type { Schema } from "../task-schema";
import { ConnectionDetail } from "../integrations/connection-detail";
import { NewMenu } from "../templates/new-menu";
import { localKeepLabel, type LocalDraftEntry } from "../local-drafts";
import { isDesktopShell } from "../export-file";
import {
  dateWithRelative,
  isoTime,
  relativeTime,
  absoluteTime,
  shortId,
} from "../format";
import {
  statusLabel,
  statusTone,
  toneAttribute,
  type Tone,
} from "../status-labels";
import { RunDetail } from "./run-detail";
import {
  columns,
  contextFacts,
  countLabel,
  rowSecondary,
  rowStatus,
  rowTitle,
  runDuration,
  runStarted,
  when,
  type ListView,
  type Pill,
  type RowContext,
} from "./records-model";

type Json = Record<string, unknown>;

/** The page's title and one-line purpose. */
const pages: Record<ListView, [string, string]> = {
  workflows: ["Workflows", "Design, publish, and activate your workflows."],
  runs: [
    "Runs",
    "Follow every run in this workspace and step in when one needs you.",
  ],
  tasks: ["My tasks", "Tasks waiting for a decision from you or your team."],
  email: [
    "Email",
    "Email threads from your workflows. Reply when a run is waiting for an answer.",
  ],
  connections: ["Connections", ""],
};
/** Local mode: what each operations page needs a platform for. */
const needPlatform: Partial<Record<ListView, [string, string]>> = {
  runs: [
    "Runs appear here once you connect",
    "Runs happen on a platform. Connect one to start and follow runs.",
  ],
  tasks: [
    "Your tasks appear here once you connect",
    "People get tasks when a run reaches a human task step.",
  ],
  email: [
    "Email threads appear here once you connect",
    "Workflows send and receive email through a platform.",
  ],
  connections: [
    "Connections live on a platform",
    "Connect to add the APIs and systems your workflows use.",
  ],
};
const icons: Record<ListView, string> = {
  workflows: "workflows",
  runs: "runs",
  tasks: "humanTask",
  email: "email",
  connections: "connections",
};
const runStatusFilters: [string, string][] = [
  ["", "All"],
  ["waiting", "Waiting"],
  ["failed", "Failed"],
  ["succeeded", "Succeeded"],
];

@Component({
  selector: "weave-records-view",
  // A view of the shell's state: checked whenever the shell renders.
  changeDetection: ChangeDetectionStrategy.Eager,
  standalone: true,
  imports: [
    FilePicker,
    Icon,
    ModalSheet,
    TaskForm,
    ConnectionDetail,
    NewMenu,
    RunDetail,
    NgTemplateOutlet,
  ],
  styleUrl: "./records-view.css",
  template: `@let h = host();
    @let v = listView();
    <div class="page-heading">
      <div>
        <h1>{{ page()[0] }}</h1>
        <p class="subtitle">{{ subtitle() }}</p>
      </div>
      <div class="tool-group heading-actions">
        @if (v === "workflows") {
          <!-- A real button: a label around a hidden input can't be reached
               from the keyboard. -->
          <button type="button" (click)="workflowFile.click()">
            <weave-icon name="upload" [size]="16" />Import workflow
          </button>
          <input
            #workflowFile
            type="file"
            accept=".yaml,.yml,.json"
            aria-label="Workflow definition file"
            (change)="h.import($event)"
            hidden
          /><span class="split-button"
            ><button class="primary" (click)="h.newWorkflow()">
              <weave-icon name="plus" [size]="16" />New workflow
            </button>
            <weave-new-menu
              label="More ways to start"
              [offerApi]="h.canCreateIntegration()"
              (choose)="h.newChoice($event)"
          /></span>
        } @else if (h.profile) {
          @if (v === "connections") {
            <button class="primary" (click)="h.openConnectionDialog()">
              <weave-icon name="plus" [size]="16" />New connection
            </button>
            @if (h.can("connection.manage")) {
              <button type="button" (click)="h.openAiConnectionDialog()">
                New AI connection
              </button>
            }
          } @else if (v === "runs" && h.can("run.start")) {
            <button class="primary" (click)="h.startRun()">
              <weave-icon name="plus" [size]="16" />Start run
            </button>
          }
          <button
            type="button"
            class="tertiary"
            (click)="h.refresh()"
            [attr.aria-disabled]="h.loading ? 'true' : null"
          >
            <weave-icon name="refresh" [size]="16" />Refresh
          </button>
        }
      </div>
    </div>
    @if (!h.profile && v !== "workflows") {
      <div class="empty-state connect-state">
        <h2>{{ platformCopy()[0] }}</h2>
        <p>{{ platformCopy()[1] }}</p>
        <button class="primary" (click)="h.openWizard('server')">
          <weave-icon name="cloud" [size]="16" />Connect to a platform
        </button>
      </div>
    } @else {
      @if (signedOut()) {
        <div class="records-banner" role="status">
          <weave-icon name="user" [size]="16" /><span
            >You're signed out of {{ h.profile!.name }}. Sign in to see runs and
            tasks.</span
          ><button
            type="button"
            (click)="h.signInPlatform(h.profile!.name, false)"
          >
            Sign in
          </button>
        </div>
      }
      @if (v === "runs") {
        <div
          class="run-filters"
          role="search"
          aria-label="Filter runs"
          [class.open]="filtersOpen"
        >
          <button
            type="button"
            class="filters-toggle"
            aria-controls="run-filter-body"
            [attr.aria-expanded]="filtersOpen"
            (click)="filtersOpen = !filtersOpen"
          >
            Filters ({{ h.runFilterCount }})
          </button>
          <div class="filter-body" id="run-filter-body">
            <label class="search-field"
              ><weave-icon name="search" [size]="16" /><input
                aria-label="Search by business key"
                placeholder="Search by business key"
                [disabled]="h.signInToRead"
                [value]="h.runFilters.business_key"
                (input)="h.setRunFilter('business_key', h.value($event))"
            /></label>
            <div class="segmented" role="group" aria-label="Status">
              @for (option of statusFilters; track option[0]) {
                <button
                  type="button"
                  [attr.aria-pressed]="h.runFilters.status === option[0]"
                  [disabled]="h.signInToRead"
                  (click)="h.setRunFilter('status', option[0])"
                >
                  {{ option[1] }}
                </button>
              }
            </div>
            <button
              type="button"
              class="tertiary"
              aria-controls="more-run-filters"
              [attr.aria-expanded]="moreFilters"
              [disabled]="h.signInToRead"
              (click)="moreFilters = !moreFilters"
            >
              More filters<weave-icon
                name="chevron"
                [size]="16"
                class="disclosure-chevron"
                [class.open]="moreFilters"
              />
            </button>
          </div>
          @if (moreFilters) {
            <div class="more-filters" id="more-run-filters">
              <div class="field">
                <label for="run-correlation">Correlation key</label>
                <input
                  id="run-correlation"
                  class="w-key"
                  [value]="h.runFilters.correlation_key"
                  (input)="h.setRunFilter('correlation_key', h.value($event))"
                />
                <p class="field-help">Matches the exact key.</p>
              </div>
              <label class="checkbox-field"
                ><input
                  type="checkbox"
                  [checked]="h.runIncludeArchived"
                  (change)="h.toggleArchivedRuns()"
                />Include archived runs</label
              >
            </div>
          }
        </div>
      }
      @if (v === "workflows") {
        <div
          class="segmented library-toggle"
          role="group"
          aria-label="Workflows"
        >
          @for (tab of libraryTabs; track tab[0]) {
            <button
              type="button"
              [class.selected]="h.libraryCollection === tab[0]"
              [attr.aria-pressed]="h.libraryCollection === tab[0]"
              (click)="chooseLibrary(tab[0])"
            >
              {{ tab[1] }}
            </button>
          }
        </div>
      }
      <div class="list-toolbar">
        @if (v !== "runs") {
          <label class="search-field"
            ><weave-icon name="search" [size]="16" /><input
              [attr.aria-label]="'Search ' + noun()"
              [placeholder]="'Search ' + noun()"
              [disabled]="h.signInToRead"
              [value]="h.search"
              (input)="h.search = h.value($event)"
          /></label>
        }
        @if (v === "tasks") {
          <select
            aria-label="Task status filter"
            [disabled]="h.signInToRead"
            (change)="h.applyTaskFilter($event)"
          >
            <option value="" [selected]="!h.taskFilter">All tasks</option>
            @for (option of taskStatuses; track option[0]) {
              <option
                [value]="option[0]"
                [selected]="h.taskFilter === option[0]"
              >
                {{ option[1] }}
              </option>
            }
          </select>
        }
        <span class="count" role="status">{{
          h.loading && !rowCount() ? "" : countLabel(v, rowCount())
        }}</span>
        @if (h.profile && h.workspaceText) {
          <span class="scope-label">{{ h.environmentLabel }}</span>
        }
      </div>
      @if (v === "tasks" && h.taskRunFilter) {
        <p class="filter-chip" role="status">
          Showing tasks for run {{ shortId(h.taskRunFilter) }}
          <button
            type="button"
            class="tertiary sm"
            (click)="h.taskRunFilter = ''"
          >
            Show all tasks
          </button>
        </p>
      }
      <!-- A reload keeps the rows it has: the table (and focus in it) stays. -->
      @if (h.loading && !localTab() && !rowCount()) {
        <div class="empty-state" role="status">
          <span class="loading-spinner"></span>
          <h2>Loading {{ noun() }}…</h2>
        </div>
      } @else if (!h.profile && v === "workflows" && !localTab()) {
        <div class="empty-state">
          <h2>
            {{
              h.libraryCollection === "drafts"
                ? "Shared drafts live on a platform"
                : "Published workflows live on a platform"
            }}
          </h2>
          <p>
            Connect to a platform to publish workflows and see your team's
            drafts. Your own work is under On this computer.
          </p>
          <button class="primary" (click)="h.openWizard('server')">
            <weave-icon name="cloud" [size]="16" />Connect to a platform
          </button>
        </div>
      } @else if (!rowCount() && h.signInToRead && !localTab()) {
        @if (h.platformNotice?.kind === "not-linked") {
          <div class="empty-state">
            <h2>Your account isn't linked yet</h2>
            <p>
              {{ h.profile!.name }} doesn't recognize your account yet. Ask your
              administrator to link it; you can keep working locally.
            </p>
          </div>
        } @else {
          <div class="empty-state">
            <h2>Sign in to see {{ noun() }}</h2>
            <p>Studio shows them as soon as you're signed in again.</p>
          </div>
        }
      } @else if (!rowCount()) {
        <div class="empty-state">
          <h2>{{ emptyHeading() }}</h2>
          <p>{{ emptyText() }}</p>
          @if (v === "workflows" && !h.search) {
            <button class="primary" (click)="h.newWorkflow()">
              <weave-icon name="plus" [size]="16" />New workflow
            </button>
          }
        </div>
      } @else {
        @if (localTab() && desktop) {
          <p class="notice local-quit-notice" data-tone="info">
            The desktop app keeps these drafts only until you quit it. Open one
            and use Save to file to keep a copy.
          </p>
        }
        <div
          class="resource-layout"
          [class.has-detail]="!!h.selectedRecord && v !== 'workflows'"
          [class.run-focus]="!!h.selectedRecord && v === 'runs'"
        >
          <div
            class="resource-table"
            role="table"
            [attr.aria-label]="page()[0]"
            [attr.data-view]="v"
          >
            <div role="rowgroup">
              <div class="table-head" role="row">
                @for (column of columnsFor(v); track column) {
                  <span role="columnheader">{{ column }}</span>
                }
                @if (localTab()) {
                  <span role="columnheader"
                    ><span class="sr-only">Actions</span></span
                  >
                }
              </div>
            </div>
            <div role="rowgroup">
              @if (localTab()) {
                @for (draft of localRows(); track draft.id) {
                  <div class="resource-row" role="row">
                    <span class="cell-main" role="cell"
                      ><span class="resource-icon" aria-hidden="true"
                        ><weave-icon name="workflows" [size]="16" /></span
                      ><span class="row-text"
                        ><button
                          type="button"
                          class="row-open"
                          (click)="h.openLocalDraft(draft.id)"
                        >
                          {{ draft.name }}</button
                        ><small>{{ keepLabel }}</small></span
                      ></span
                    ><span role="cell"
                      ><span class="tag">{{ draft.version || "—" }}</span></span
                    ><span role="cell" class="cell-status"
                      ><span class="status-pill">Local draft</span></span
                    ><span role="cell"
                      ><time
                        [attr.datetime]="iso(draft.savedAt)"
                        [attr.title]="absolute(draft.savedAt)"
                        >Edited {{ relative(draft.savedAt) }}</time
                      ></span
                    ><span role="cell" class="cell-actions"
                      ><button
                        type="button"
                        class="tertiary danger sm"
                        [attr.aria-label]="
                          'Delete ' + draft.name + ' from this computer'
                        "
                        (click)="h.deleteLocalDraft(draft)"
                      >
                        <weave-icon name="trash" [size]="16" />Delete
                      </button></span
                    >
                  </div>
                }
              } @else {
                @for (record of h.visibleRecords; track record["id"]) {
                  @let selected = h.selectedRecord?.["id"] === record["id"];
                  @let status = pill(v, record);
                  <div
                    class="resource-row"
                    role="row"
                    [class.selected]="selected"
                  >
                    <span class="cell-main" role="cell"
                      ><span class="resource-icon" aria-hidden="true"
                        ><weave-icon [name]="icon(v)" [size]="16" /></span
                      ><span class="row-text"
                        ><button
                          type="button"
                          class="row-open"
                          [attr.aria-current]="selected ? 'true' : null"
                          (click)="h.open(record)"
                        >
                          {{ rowTitle(v, record, context()) }}
                        </button>
                        @if (rowSecondary(v, record, context()); as second) {
                          <small>{{ second }}</small>
                        }
                      </span></span
                    >
                    @switch (v) {
                      @case ("workflows") {
                        <span role="cell"
                          ><span class="tag">{{
                            text(record["version"]) || docVersion(record) || "—"
                          }}</span></span
                        ><span role="cell" class="cell-status">
                          <ng-container
                            *ngTemplateOutlet="
                              pillTemplate;
                              context: { $implicit: status }
                            " /></span
                        ><span role="cell">{{
                          when(record["updated_at"] ?? record["created_at"])
                        }}</span>
                      }
                      @case ("runs") {
                        <span role="cell" class="cell-status">
                          <ng-container
                            *ngTemplateOutlet="
                              pillTemplate;
                              context: { $implicit: status }
                            " /></span
                        ><span role="cell"
                          ><time
                            [attr.datetime]="iso(started(record))"
                            [attr.title]="absolute(started(record))"
                            >{{ when(started(record)) }}</time
                          ></span
                        ><span role="cell">{{ duration(record) || "—" }}</span>
                      }
                      @case ("tasks") {
                        <span role="cell" class="cell-status">
                          <ng-container
                            *ngTemplateOutlet="
                              pillTemplate;
                              context: { $implicit: status }
                            " /></span
                        ><span role="cell">
                          @if (record["due_at"]) {
                            <time
                              [attr.datetime]="iso(record['due_at'])"
                              [attr.title]="absolute(record['due_at'])"
                              >{{ when(record["due_at"]) }}</time
                            >
                          } @else {
                            —
                          }
                        </span>
                      }
                      @case ("connections") {
                        <span role="cell">{{
                          text(record["connector"]) ? connector(record) : "—"
                        }}</span
                        ><span role="cell">{{
                          record["revision"]
                            ? "Revision " + record["revision"]
                            : "—"
                        }}</span>
                      }
                      @case ("email") {
                        <span role="cell"
                          ><time
                            [attr.datetime]="iso(lastMessage(record))"
                            [attr.title]="absolute(lastMessage(record))"
                            >{{ when(lastMessage(record)) }}</time
                          ></span
                        ><span role="cell" class="cell-status">
                          <ng-container
                            *ngTemplateOutlet="
                              pillTemplate;
                              context: { $implicit: status }
                            "
                        /></span>
                      }
                    }
                  </div>
                }
              }
            </div>
          </div>
          @if (h.nextCursor && !localTab()) {
            <button class="load-more" (click)="h.refresh(true)">
              Load more {{ noun() }}
            </button>
          }
          @if (h.selectedRecord && v !== "workflows") {
            @let record = h.selectedRecord;
            <section
              class="record-detail"
              role="complementary"
              aria-labelledby="record-detail-title"
              [attr.data-view]="v"
              [weaveModalSheet]="true"
              [sheetWhen]="sheetWhen.detail"
              sheetInitialFocus="#record-detail-title"
              (sheetDismiss)="h.closeRecord()"
            >
              @if (v === "runs") {
                <weave-run-detail [host]="h" />
              } @else {
                <header class="detail-header">
                  <h2 id="record-detail-title" tabindex="-1">
                    {{ rowTitle(v, record, context()) }}
                  </h2>
                  <button
                    class="icon-button"
                    aria-label="Close detail"
                    (click)="h.closeRecord()"
                  >
                    <weave-icon name="close" />
                  </button>
                </header>
                @if (pill(v, record); as status) {
                  <p class="detail-status">
                    <span
                      class="status-pill"
                      [attr.data-tone]="tone(status.tone)"
                      >{{ status.label }}</span
                    >
                  </p>
                }
                @switch (v) {
                  @case ("tasks") {
                    <ng-container *ngTemplateOutlet="taskDetail" />
                  }
                  @case ("email") {
                    <ng-container *ngTemplateOutlet="emailDetail" />
                  }
                  @case ("connections") {
                    @if (record["unavailable"]) {
                      <p class="notice">
                        Studio can't show this connection. Its connector may
                        have been retired, or it was created by an older
                        platform version.
                      </p>
                    } @else {
                      <weave-connection-detail
                        [api]="h.api"
                        [connection]="record"
                        [canCheck]="
                          h.can('connection.manage', text(record['id']))
                        "
                      />
                    }
                  }
                }
                @if (!record["unavailable"]) {
                  <div class="action-row detail-refresh">
                    <button
                      type="button"
                      class="tertiary"
                      (click)="h.readDetail()"
                    >
                      <weave-icon name="refresh" [size]="16" />Refresh
                    </button>
                  </div>
                }
              }
            </section>
          }
        </div>
      }
    }
    <ng-template #pillTemplate let-status>
      @if (status) {
        <span class="status-pill" [attr.data-tone]="tone(status.tone)">{{
          status.label
        }}</span>
      } @else {
        <span class="muted">—</span>
      }
    </ng-template>
    <ng-template #taskDetail>
      @let record = h.selectedRecord!;
      @let facts = taskFacts();
      <dl class="task-metadata">
        @for (fact of facts.facts; track fact.label) {
          <dt>{{ fact.label }}</dt>
          <dd>{{ fact.value }}</dd>
        }
        <dt>Due</dt>
        <dd>
          @if (record["due_at"]) {
            <time [attr.datetime]="iso(record['due_at'])">{{
              dateWithRelative(record["due_at"])
            }}</time>
          } @else {
            No due date
          }
        </dd>
        @if (record["expires_at"]) {
          <dt>Expires</dt>
          <dd>
            <time [attr.datetime]="iso(record['expires_at'])">{{
              dateWithRelative(record["expires_at"])
            }}</time>
          </dd>
        }
        @if (record["decision_actor_id"]) {
          <dt>Decided by</dt>
          <dd>
            {{
              record["decision_actor_id"] === h.identity?.principal_id
                ? "You"
                : "Someone else"
            }}
          </dd>
        }
      </dl>
      @for (
        attachment of taskFiles();
        track attachment.label + attachment.file.id
      ) {
        @defer (on immediate) {
          <weave-file-picker
            [value]="attachment.file"
            [access]="h.taskFileAccess"
            [label]="attachment.label"
            [readOnly]="true"
          />
        }
      }
      @if (hasKeys(facts.nested)) {
        <details class="disclosure">
          <summary>Technical details</summary>
          <pre>{{ h.pretty(facts.nested) }}</pre>
        </details>
      }
      @if (
        record["status"] === "ready" &&
        h.can("human_task.claim", text(record["id"]))
      ) {
        <div class="action-row">
          <button
            class="primary"
            [disabled]="h.busy !== '' || h.unknownCommand || h.taskConflict"
            (click)="h.taskCommand('claim')"
          >
            Claim task
          </button>
        </div>
      }
      @if (record["status"] === "claimed" && h.taskOwned()) {
        <form class="human-decision-form" (submit)="$event.preventDefault()">
          <h3>Your decision</h3>
          <fieldset [disabled]="h.taskConflict">
            @for (taskId of [text(record["id"])]; track taskId) {
              <weave-task-form
                [fileAccess]="h.taskFileAccess"
                [schema]="taskSchema()"
                (dataChange)="h.taskData = $event"
                (validityChange)="h.taskFormValid = $event"
              />
            }
          </fieldset>
          @if (h.taskConfirm; as decision) {
            <div
              class="decision-confirm"
              role="group"
              aria-labelledby="decision-confirm-text"
            >
              <p id="decision-confirm-text">
                {{ verb(decision) }} {{ record["title"] || "this task" }}? You
                can't change this later.
              </p>
              <div class="action-row">
                <button
                  type="button"
                  data-initial-focus
                  [class.primary]="decision !== 'reject'"
                  [class.danger]="decision === 'reject'"
                  [class.solid]="decision === 'reject'"
                  [disabled]="h.busy !== '' || h.unknownCommand"
                  (click)="h.taskCommand('complete', decision)"
                >
                  {{ verb(decision) }}
                </button>
                <button type="button" (click)="h.cancelDecision()">Back</button>
              </div>
            </div>
          } @else {
            <div class="decision-bar">
              @for (decision of h.taskDecisions(); track decision) {
                @if (h.can("human_task.complete", text(record["id"]))) {
                  <button
                    type="button"
                    [attr.data-decision]="decision"
                    [class.primary]="decision !== 'reject'"
                    [class.danger]="decision === 'reject'"
                    [disabled]="
                      h.busy !== '' ||
                      h.unknownCommand ||
                      h.taskConflict ||
                      !h.taskFormValid
                    "
                    [attr.aria-describedby]="
                      h.taskFormValid ? null : 'task-form-invalid'
                    "
                    (click)="h.askDecision(decision)"
                  >
                    {{ verb(decision) }}
                  </button>
                }
              }
            </div>
            @if (!h.taskFormValid) {
              <p class="field-error" id="task-form-invalid">
                Correct the fields marked with an error.
              </p>
            }
          }
        </form>
        @if (h.can("human_task.release", text(record["id"]))) {
          <div class="action-row">
            <button
              [disabled]="h.busy !== '' || h.unknownCommand || h.taskConflict"
              (click)="h.taskCommand('release')"
            >
              Release task
            </button>
          </div>
        }
      } @else if (record["status"] === "completed") {
        <h3>Recorded outcome</h3>
        @let output = outputFacts();
        <dl class="task-metadata">
          @if (output.decision) {
            <dt>Decision</dt>
            <dd>{{ verb(output.decision) }}</dd>
          }
          @for (fact of output.facts; track fact.label) {
            <dt>{{ fact.label }}</dt>
            <dd>{{ fact.value }}</dd>
          }
        </dl>
      } @else if (record["status"] === "claimed") {
        <p class="hint">
          Claimed by someone else. It comes back to My tasks if they release it.
        </p>
      }
    </ng-template>
    <ng-template #emailDetail>
      @let record = h.selectedRecord!;
      <div class="email-thread">
        @for (message of h.messages(); track message["id"]) {
          <article
            class="mail-message"
            [class.outbound]="message['direction'] === 'outbound'"
          >
            <header>
              <strong>{{ h.mail(message)["sender"] }}</strong
              ><span
                class="status-pill"
                [attr.data-tone]="emailTone(message['state'])"
                >{{ emailLabel(message["state"]) }}</span
              >
            </header>
            <p class="mail-meta">
              To: {{ recipients(message) }} ·
              <time [attr.datetime]="iso(message['accepted_at'])">{{
                absolute(message["accepted_at"]) || text(message["accepted_at"])
              }}</time>
            </p>
            <h3>{{ h.mail(message)["subject"] }}</h3>
            <p class="mail-body">{{ h.mail(message)["text"] }}</p>
          </article>
        }
      </div>
      <p class="hint">Mail servers don't confirm delivery or reading.</p>
      @if (h.emailSubmission; as submission) {
        <div class="submission-status" role="status">
          @switch (submission["state"]) {
            @case ("unknown") {
              <div class="notice">
                <p>
                  We couldn't confirm it was sent. Check the status before
                  sending again.
                </p>
                <button type="button" (click)="h.reconcileEmail()">
                  Check status
                </button>
              </div>
            }
            @case ("queued") {
              <p>
                <span class="status-pill" data-tone="warning"
                  >Waiting to send</span
                >
              </p>
              @if (h.can("email.send")) {
                <button
                  type="button"
                  [disabled]="h.busy !== '' || h.unknownCommand"
                  (click)="h.executeEmail()"
                >
                  Send now
                </button>
              }
            }
            @default {
              <p>
                <span
                  class="status-pill"
                  [attr.data-tone]="emailTone(submission['state'])"
                  >{{ emailLabel(submission["state"]) }}</span
                >
              </p>
            }
          }
        </div>
      }
      @if (h.can("email.send", text(record["id"]))) {
        <div class="field reply-field">
          <label for="email-reply">Reply</label>
          <textarea
            id="email-reply"
            class="reply-editor"
            [value]="h.reply"
            (input)="h.reply = h.value($event)"
            placeholder="Write a plain-text reply"
            aria-label="Email reply"
          ></textarea>
        </div>
        <div class="action-row">
          <button
            class="primary"
            [disabled]="!h.reply.trim() || h.busy !== '' || h.unknownCommand"
            (click)="h.sendReply()"
          >
            Send reply
          </button>
        </div>
      }
    </ng-template>`,
})
export class RecordsView {
  /** The editor shell, which owns every list and command. */
  host = input.required<App>();
  readonly sheetWhen = sheetWhen;
  readonly statusFilters = runStatusFilters;
  readonly taskStatuses: [string, string][] = [
    ["ready", "Ready to claim"],
    ["claimed", "Claimed"],
    ["completed", "Completed"],
    ["expired", "Expired"],
    ["cancelled", "Canceled"],
  ];
  readonly libraryTabs: ["workflows" | "drafts" | "local", string][] = [
    ["workflows", "Published"],
    ["drafts", "Drafts"],
    ["local", "On this computer"],
  ];
  readonly countLabel = countLabel;
  readonly rowTitle = rowTitle;
  readonly rowSecondary = rowSecondary;
  readonly rowStatus = rowStatus;
  /** One object per label and tone, so template bindings stay stable. */
  private pills = new Map<string, Pill | null>();
  pill(view: ListView, record: Json): Pill | null {
    const status = rowStatus(view, record, this.context());
    const key = status ? `${status.label}|${status.tone}` : "";
    if (!this.pills.has(key)) this.pills.set(key, status);
    return this.pills.get(key)!;
  }
  readonly when = (value: unknown) => when(value);
  readonly shortId = shortId;
  readonly dateWithRelative = (value: unknown) => dateWithRelative(value);
  /** Narrow windows fold the Runs filters behind "Filters (n)". */
  filtersOpen = false;
  moreFilters = false;
  private contextCache: { key: unknown[]; value: RowContext } = {
    key: [],
    value: {},
  };

  listView(): ListView {
    const view = this.host().view as View;
    return (
      ["workflows", "runs", "tasks", "email", "connections"] as const
    ).includes(view as ListView)
      ? (view as ListView)
      : "workflows";
  }
  page() {
    return pages[this.listView()];
  }
  subtitle() {
    const h = this.host();
    if (this.listView() !== "connections") return this.page()[1];
    return `The APIs and systems your workflows use${
      h.workspaceText ? ` in ${h.workspaceText}` : ""
    }. You choose one for each connection slot when you activate a version.`;
  }
  platformCopy() {
    return needPlatform[this.listView()] ?? ["", ""];
  }
  noun() {
    return countLabel(this.listView(), 2).replace(/^2 /, "");
  }
  columnsFor(view: ListView) {
    return columns[view];
  }
  icon(view: ListView) {
    return icons[view];
  }
  /** The person is signed out (not expired): a calm banner offers Sign in. */
  signedOut() {
    const h = this.host();
    return h.platformSignedOut && !h.platformNotice;
  }
  /** The desktop app's window keeps no browser data after it quits. */
  readonly desktop = isDesktopShell();
  readonly keepLabel = localKeepLabel(this.desktop);
  localTab() {
    return (
      this.listView() === "workflows" &&
      this.host().libraryCollection === "local"
    );
  }
  localRows(): LocalDraftEntry[] {
    const query = this.host().search.toLowerCase();
    return this.host().localList.filter(
      (draft) => !query || draft.name.toLowerCase().includes(query),
    );
  }
  rowCount() {
    return this.localTab()
      ? this.localRows().length
      : this.host().visibleRecords.length;
  }
  chooseLibrary(tab: "workflows" | "drafts" | "local") {
    const h = this.host();
    if (h.libraryCollection === tab) return;
    h.libraryCollection = tab;
    h.records = [];
    h.nextCursor = null;
    void h.refresh();
  }
  emptyHeading() {
    const h = this.host();
    if (h.search) return "No matching results";
    if (this.localTab()) return "Nothing on this computer yet";
    return {
      workflows: "No workflows yet",
      runs: h.runFilterCount ? "No runs match these filters" : "No runs yet",
      tasks: "No tasks for you right now",
      email: "No email threads yet",
      connections: "No connections yet",
    }[this.listView()];
  }
  emptyText() {
    const h = this.host();
    if (h.search) return "Try a different search.";
    if (this.localTab())
      return this.desktop
        ? "Workflows you create here are kept until you quit Firefly Weave Studio. Save to file to keep a copy."
        : "Workflows you create here are kept in this browser until you publish them.";
    return {
      workflows: "Start from a blank canvas or a template.",
      runs: h.runFilterCount
        ? "Change or clear the filters to see more runs."
        : "Activate a workflow version, then start a run to follow it here.",
      tasks:
        "People get tasks when a run reaches a human task step. They appear here when they're ready to claim.",
      email: "Threads appear when a workflow sends or receives email.",
      connections:
        "Add a connection for each API or system your workflows call.",
    }[this.listView()];
  }
  /** The row context, rebuilt only when what it depends on changes. */
  context(): RowContext {
    const h = this.host();
    const key = [
      h.identity,
      h.workflowVersions.size,
      h.activationsByVersion,
      h.workspaceNames?.environment,
      h.libraryCollection,
      h.knownRuns.size,
    ];
    if (key.some((part, i) => part !== this.contextCache.key[i]))
      this.contextCache = {
        key,
        value: {
          principal: h.identity?.principal_id ?? null,
          versions: h.workflowVersions,
          activations: h.activationsByVersion,
          environment: h.workspaceNames?.environment ?? "",
          library: h.libraryCollection,
          runs: h.knownRuns,
        },
      };
    return this.contextCache.value;
  }
  tone(tone: Tone) {
    return toneAttribute(tone);
  }
  text(value: unknown) {
    return typeof value === "string" ? value : "";
  }
  docVersion(record: Json) {
    const document = record["document"] as
      | { metadata?: { version?: string } }
      | undefined;
    return document?.metadata?.version ?? "";
  }
  connector(record: Json) {
    return rowSecondary("connections", record);
  }
  started(record: Json) {
    return runStarted(record);
  }
  duration(record: Json) {
    return runDuration(record);
  }
  lastMessage(record: Json) {
    return (
      record["last_message_at"] ?? record["updated_at"] ?? record["created_at"]
    );
  }
  iso(value: unknown) {
    return isoTime(value) || null;
  }
  absolute(value: unknown) {
    return absoluteTime(value);
  }
  relative(value: unknown) {
    return relativeTime(value) || "recently";
  }
  hasKeys(value: Json) {
    return Object.keys(value).length > 0;
  }
  taskSchema() {
    return (this.host().selectedRecord?.["form_schema"] ?? {}) as Schema;
  }
  taskFacts() {
    return contextFacts(this.host().selectedRecord?.["context"]);
  }
  taskFiles() {
    const record = this.host().selectedRecord;
    return [
      ...filesIn(record?.["context"], "Context"),
      ...filesIn(record?.["output"], "Answer"),
    ];
  }
  outputFacts() {
    const output = (this.host().selectedRecord?.["output"] ?? {}) as Json;
    return {
      decision: this.text(output["decision"]),
      facts: contextFacts(output["data"] ?? {}).facts,
    };
  }
  /** "approve" reads "Approve". */
  verb(decision: string) {
    return decision.charAt(0).toUpperCase() + decision.slice(1);
  }
  emailLabel(state: unknown) {
    return statusLabel("email", state);
  }
  emailTone(state: unknown) {
    return toneAttribute(statusTone("email", state));
  }
  recipients(message: Json) {
    const to = this.host().mail(message)["to"];
    return Array.isArray(to) ? to.join(", ") : this.text(to);
  }
}
