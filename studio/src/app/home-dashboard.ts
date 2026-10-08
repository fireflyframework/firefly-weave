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
// Home. Connected, it leads with what needs you: tasks ready to claim, and
// failed or waiting runs, each row opening the item itself. Locally, a
// compact welcome with the ways to start and the work kept on this computer.
import { Component, input, output } from "@angular/core";
import { Icon } from "./icon";
import type { LocalDraftEntry } from "./local-drafts";
import { absoluteTime, isoTime, relativeTime, shortId } from "./format";
import {
  runStatus,
  statusLabel,
  statusTone,
  toneAttribute,
  type StatusKind,
} from "./status-labels";

type Json = Record<string, unknown>;
const text = (value: unknown) => (typeof value === "string" ? value : "");

@Component({
  selector: "weave-home-dashboard",
  standalone: true,
  imports: [Icon],
  styleUrl: "./home-dashboard.css",
  template: `<section
    class="home"
    [class.local]="!connected()"
    aria-labelledby="home-title"
  >
    <header
      class="home-header"
      [class.dragging]="dragging"
      (dragover)="dragOver($event)"
      (dragleave)="dragLeave($event)"
      (drop)="drop($event)"
    >
      <div class="home-intro">
        <p class="dashboard-status">
          {{
            connected()
              ? workspaceName() || "Your workspace"
              : "Local authoring"
          }}
        </p>
        @if (connected()) {
          <h1 id="home-title">Home</h1>
        } @else {
          <h1 id="home-title" class="display">Welcome to Weave Studio</h1>
          <p class="welcome-description">
            Create, import, and check workflows on this computer. Connect to a
            platform when you're ready to publish and run them.
          </p>
        }
      </div>
      <div class="home-tools">
        <div class="home-actions">
          <button type="button" class="primary" (click)="createWorkflow.emit()">
            <weave-icon name="plus" [size]="16" />New workflow
          </button>
          <input
            #filePicker
            class="file-picker"
            type="file"
            accept=".yaml,.yml,.json"
            aria-label="Choose a workflow file"
            (change)="choose($event)"
          />
          <button
            type="button"
            aria-describedby="import-note"
            (click)="filePicker.click()"
          >
            <weave-icon name="upload" [size]="16" />Import workflow
          </button>
          @if (!connected()) {
            <button type="button" (click)="connect.emit()">
              <weave-icon name="cloud" [size]="16" />Connect to a platform
            </button>
          }
        </div>
        <p class="import-note" id="import-note">
          YAML or JSON, up to 1 MiB. You can also drop it here.
        </p>
        @if (importError) {
          <p class="import-error" role="alert">{{ importError }}</p>
        }
      </div>
    </header>
    @if (resume(); as latest) {
      <button
        type="button"
        class="continue-row"
        (click)="continueEditing.emit()"
      >
        <span class="row-icon"
          ><weave-icon name="workflows" [size]="20"
        /></span>
        <span class="row-copy"
          ><span
            >Continue editing <strong>{{ latest.name }}</strong
            >, edited {{ relative(latest.savedAt) }}</span
          ></span
        ><weave-icon name="chevron" [size]="16" />
      </button>
    }
    @if (connected()) {
      <section
        class="home-section needs-you"
        aria-labelledby="needs-title"
        [attr.aria-busy]="loading()"
      >
        <header class="section-heading">
          <h2 id="needs-title">Needs you</h2>
        </header>
        @if (loading()) {
          <p class="empty" role="status">Loading what needs you…</p>
        } @else if (attention().length) {
          <ul class="preview-list">
            @for (item of attention(); track $index) {
              <li>
                <button
                  type="button"
                  class="preview-row"
                  (click)="
                    item.kind === 'task'
                      ? openTask.emit(item.record)
                      : openRun.emit(item.record)
                  "
                >
                  <span class="row-icon"
                    ><weave-icon
                      [name]="item.kind === 'task' ? 'humanTask' : 'runs'"
                      [size]="16"
                  /></span>
                  <span class="row-copy"
                    ><strong>{{ title(item.kind, item.record) }}</strong
                    ><span>{{ secondary(item.kind, item.record) }}</span></span
                  ><span
                    class="status-pill"
                    [attr.data-tone]="tone(item.kind, item.record)"
                    >{{ label(item.kind, item.record) }}</span
                  >
                </button>
              </li>
            }
          </ul>
        } @else if (signInToRead()) {
          <p class="empty">Sign in to see what needs you.</p>
        } @else {
          <p class="empty">
            Nothing needs you right now. Tasks ready to claim and runs that
            failed or wait appear here.
          </p>
        }
      </section>
    }
    <div class="home-columns" [class.single]="!connected()">
      <section class="home-section" aria-labelledby="workflows-title">
        <header class="section-heading">
          <h2 id="workflows-title">Recent workflows</h2>
          <button
            type="button"
            class="text-button"
            (click)="navigate.emit('workflows')"
          >
            View all workflows<weave-icon name="chevron" [size]="16" />
          </button>
        </header>
        @if (localDrafts().length || drafts().length) {
          <ul class="preview-list">
            @for (draft of localDrafts().slice(0, 5); track draft.id) {
              <li>
                <button
                  type="button"
                  class="preview-row"
                  (click)="openLocal.emit(draft.id)"
                >
                  <span class="row-icon"
                    ><weave-icon name="workflows" [size]="16"
                  /></span>
                  <span class="row-copy"
                    ><strong>{{ draft.name }}</strong
                    ><span
                      >On this computer · edited
                      <time
                        [attr.datetime]="iso(draft.savedAt)"
                        [attr.title]="absolute(draft.savedAt)"
                        >{{ relative(draft.savedAt) }}</time
                      ></span
                    ></span
                  >
                  @if (draft.version) {
                    <span class="tag">{{ draft.version }}</span>
                  }
                </button>
              </li>
            }
            @for (draft of drafts().slice(0, 5); track draft["id"]) {
              <li>
                <button
                  type="button"
                  class="preview-row"
                  (click)="openDraft.emit(draft)"
                >
                  <span class="row-icon"
                    ><weave-icon name="workflows" [size]="16"
                  /></span>
                  <span class="row-copy"
                    ><strong>{{ draftName(draft) }}</strong
                    ><span>Draft on the platform</span></span
                  >
                </button>
              </li>
            }
          </ul>
        } @else {
          <p class="empty">
            Workflows you create or open appear here. Start with New workflow or
            a template below.
          </p>
        }
      </section>
      @if (connected()) {
        <section
          class="home-section"
          aria-labelledby="runs-title"
          [attr.aria-busy]="loading()"
        >
          <header class="section-heading">
            <h2 id="runs-title">Recent runs</h2>
            <button
              type="button"
              class="text-button"
              (click)="navigate.emit('runs')"
            >
              View all runs<weave-icon name="chevron" [size]="16" />
            </button>
          </header>
          @if (loading()) {
            <p class="empty" role="status">Loading recent runs…</p>
          } @else if (runs().length) {
            <ul class="preview-list">
              @for (run of runs().slice(0, 5); track run["id"]) {
                <li>
                  <button
                    type="button"
                    class="preview-row"
                    (click)="openRun.emit(run)"
                  >
                    <span class="row-icon"
                      ><weave-icon name="runs" [size]="16"
                    /></span>
                    <span class="row-copy"
                      ><strong>{{ title("run", run) }}</strong
                      ><span>{{ secondary("run", run) }}</span></span
                    ><span
                      class="status-pill"
                      [attr.data-tone]="tone('run', run)"
                      >{{ label("run", run) }}</span
                    >
                  </button>
                </li>
              }
            </ul>
          } @else if (signInToRead()) {
            <p class="empty">Sign in to see runs.</p>
          } @else {
            <p class="empty">
              No runs yet. Activate a workflow version, then start a run to
              follow it here.
            </p>
          }
        </section>
        <section
          class="home-section"
          aria-labelledby="tasks-title"
          [attr.aria-busy]="loading()"
        >
          <header class="section-heading">
            <h2 id="tasks-title">My tasks</h2>
            <button
              type="button"
              class="text-button"
              (click)="navigate.emit('tasks')"
            >
              View all tasks<weave-icon name="chevron" [size]="16" />
            </button>
          </header>
          @if (loading()) {
            <p class="empty" role="status">Loading your tasks…</p>
          } @else if (tasks().length) {
            <ul class="preview-list">
              @for (task of tasks().slice(0, 5); track task["id"]) {
                <li>
                  <button
                    type="button"
                    class="preview-row"
                    (click)="openTask.emit(task)"
                  >
                    <span class="row-icon"
                      ><weave-icon name="humanTask" [size]="16"
                    /></span>
                    <span class="row-copy"
                      ><strong>{{ title("task", task) }}</strong
                      ><span>{{ secondary("task", task) }}</span></span
                    ><span
                      class="status-pill"
                      [attr.data-tone]="tone('task', task)"
                      >{{ label("task", task) }}</span
                    >
                  </button>
                </li>
              }
            </ul>
          } @else if (signInToRead()) {
            <p class="empty">Sign in to see your tasks.</p>
          } @else {
            <p class="empty">
              No tasks claimed by you. Claim one from Needs you or My tasks.
            </p>
          }
        </section>
      }
    </div>
  </section>`,
})
export class HomeDashboard {
  connected = input(false);
  workspaceName = input("");
  /** Tasks claimed by the signed-in person. */
  tasks = input<Json[]>([]);
  /** The most recent runs. */
  runs = input<Json[]>([]);
  /** "Needs you": tasks ready to claim, failed runs, waiting runs. */
  attention = input<{ kind: "task" | "run"; record: Json }[]>([]);
  /** Drafts on the platform. */
  drafts = input<Json[]>([]);
  /** Workflows kept on this computer, newest first. */
  localDrafts = input<LocalDraftEntry[]>([]);
  /** "Continue editing": the open workflow, or the newest kept one. */
  resume = input<{ name: string; savedAt: string } | null>(null);
  /** Published versions by ID, for run titles. */
  versions = input<ReadonlyMap<string, { name: string; version: string }>>(
    new Map(),
  );
  principal = input("");
  loading = input(false);
  /** The platform is saved but not signed in: previews can't be read yet. */
  signInToRead = input(false);
  createWorkflow = output<void>();
  importWorkflow = output<File>();
  connect = output<void>();
  navigate = output<"workflows" | "tasks" | "runs">();
  openLocal = output<string>();
  continueEditing = output<void>();
  openDraft = output<Json>();
  openRun = output<Json>();
  openTask = output<Json>();
  dragging = false;
  importError = "";
  choose(event: Event) {
    const picker = event.target as HTMLInputElement;
    this.accept(picker.files);
    picker.value = "";
  }
  dragOver(event: DragEvent) {
    event.preventDefault();
    this.dragging = true;
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  }
  dragLeave(event: DragEvent) {
    const target = event.currentTarget as HTMLElement;
    if (
      !(event.relatedTarget instanceof Node) ||
      !target.contains(event.relatedTarget)
    )
      this.dragging = false;
  }
  drop(event: DragEvent) {
    event.preventDefault();
    this.dragging = false;
    this.accept(event.dataTransfer?.files ?? null);
  }
  private accept(files: FileList | null) {
    if (!files?.length) return;
    if (files.length !== 1) {
      this.importError = "Choose one workflow file at a time.";
      return;
    }
    const file = files[0];
    if (!/\.(yaml|yml|json)$/i.test(file.name)) {
      this.importError = "Choose a .yaml, .yml or .json workflow file.";
      return;
    }
    if (file.size > 1024 * 1024) {
      this.importError =
        "This file is larger than 1 MiB. Choose a smaller workflow file.";
      return;
    }
    this.importError = "";
    this.importWorkflow.emit(file);
  }
  private kind(kind: "task" | "run"): StatusKind {
    return kind;
  }
  /** A run reads "{workflow} {version}"; a task its title. */
  title(kind: "task" | "run", record: Json) {
    if (kind === "task") return text(record["title"]) || "Human task";
    const activation = (record["activation"] ?? {}) as Json;
    const request = (activation["request"] ?? {}) as Json;
    const known = this.versions().get(text(request["version_id"]));
    const workflow = known
      ? `${known.name} ${known.version}`.trim()
      : text(activation["name"]);
    return (
      workflow || text(record["business_key"]) || `Run ${shortId(record["id"])}`
    );
  }
  secondary(kind: "task" | "run", record: Json) {
    if (kind === "task") {
      const due = relativeTime(record["due_at"]);
      return due ? `Due ${due}` : "No due date";
    }
    const key = text(record["business_key"]);
    const short = `Run ${shortId(record["id"])}`;
    return this.title("run", record) === key ? short : key || short;
  }
  private status(kind: "task" | "run", record: Json) {
    return kind === "run" ? runStatus(record) : record["status"];
  }
  label(kind: "task" | "run", record: Json) {
    return statusLabel(this.kind(kind), this.status(kind, record), {
      mine: !!this.principal() && record["claimant_id"] === this.principal(),
    });
  }
  tone(kind: "task" | "run", record: Json) {
    return toneAttribute(
      statusTone(this.kind(kind), this.status(kind, record)),
    );
  }
  draftName(draft: Json) {
    const document = (draft["document"] ?? {}) as {
      metadata?: { name?: string; version?: string };
    };
    const metadata = document.metadata;
    return metadata?.name
      ? `${metadata.name} ${metadata.version ?? ""}`.trim()
      : "untitled-workflow";
  }
  relative(value: string) {
    return relativeTime(value) || "recently";
  }
  absolute(value: string) {
    return absoluteTime(value);
  }
  iso(value: string) {
    return isoTime(value) || null;
  }
}
