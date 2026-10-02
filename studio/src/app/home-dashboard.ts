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
import { Component, input, output } from "@angular/core";
import { Icon } from "./icon";

@Component({
  selector: "weave-home-dashboard",
  standalone: true,
  imports: [Icon],
  styleUrl: "./home-dashboard.css",
  template: `
    <section class="home" aria-labelledby="home-title">
      <header class="welcome">
        <div>
          <p class="dashboard-status">
            {{
              connected()
                ? workspaceName() || "Your workspace"
                : "Not connected to a platform"
            }}
          </p>
          <h1 id="home-title">Welcome to Weave Studio</h1>
          <p class="welcome-description">
            {{
              connected()
                ? "Turn a process into a workflow. Start with a blank canvas, or bring a definition you already have."
                : "Create, import, and validate workflows on this computer. Connect to save them to a platform, run processes, or handle tasks."
            }}
          </p>
        </div>
        <img
          class="lumi"
          src="/assets/lumi.svg"
          alt="Lumi, the Firefly guide"
          width="136"
          height="136"
        />
      </header>
      <div class="start-grid">
        <section class="create-panel" aria-labelledby="create-title">
          <span class="panel-icon"><weave-icon name="workflows" /></span>
          <h2 id="create-title">Create a workflow</h2>
          <p>
            Build a process on the canvas. Add actions, decisions and human
            work, then validate it before execution.
          </p>
          <button type="button" class="primary" (click)="createWorkflow.emit()">
            <weave-icon name="plus" />New workflow
          </button>
          <span class="panel-note">Begin with an editable draft</span>
        </section>
        <section
          class="import-panel"
          [class.dragging]="dragging"
          aria-labelledby="import-title"
          (dragover)="dragOver($event)"
          (dragleave)="dragLeave($event)"
          (drop)="drop($event)"
        >
          <span class="panel-icon"><weave-icon name="source" /></span>
          <h2 id="import-title">Import a definition</h2>
          <p>
            Open a Weave YAML or JSON file to continue editing its process and
            source.
          </p>
          <input
            #filePicker
            class="file-picker"
            type="file"
            accept=".yaml,.yml,.json"
            aria-label="Choose a workflow definition"
            (change)="choose($event)"
          />
          <button type="button" (click)="filePicker.click()">
            <weave-icon name="download" />Choose YAML or JSON
          </button>
          <span class="panel-note">Or drop one file here · up to 1 MiB</span>
          @if (importError) {
            <p class="import-error" role="alert">{{ importError }}</p>
          }
          <p class="import-help">
            Import opens a draft for review. Publishing and activation are
            separate steps.
          </p>
        </section>
      </div>
      @if (connected()) {
        <div class="work-grid" [attr.aria-busy]="loading()">
          <section class="work-panel" aria-labelledby="inbox-title">
            <header class="section-heading">
              <div>
                <h2 id="inbox-title">Your human work</h2>
                <p>Review context and decide from your task inbox.</p>
              </div>
              <button
                type="button"
                class="text-button"
                (click)="navigate.emit('tasks')"
              >
                Open inbox<weave-icon name="chevron" />
              </button>
            </header>
            @if (loading()) {
              <p class="empty">Loading your task preview…</p>
            } @else if (tasks().length) {
              <ul class="preview-list">
                @for (task of tasks().slice(0, 3); track task["id"]) {
                  <li>
                    <button
                      type="button"
                      class="preview-row"
                      (click)="navigate.emit('tasks')"
                    >
                      <span class="row-icon"
                        ><weave-icon name="humanTask"
                      /></span>
                      <span class="row-copy"
                        ><strong>{{ task["title"] || "Human task" }}</strong
                        ><span>{{
                          task["claimant_id"]
                            ? "Claimed for review"
                            : "Ready to claim"
                        }}</span></span
                      ><weave-icon name="chevron" />
                    </button>
                  </li>
                }
              </ul>
            } @else {
              <div class="empty">
                <weave-icon name="tasks" />
                <p>No human tasks in this preview.</p>
                <span
                  >Your inbox shows work available to your identity and
                  scope.</span
                >
              </div>
            }
          </section>
          <section class="work-panel" aria-labelledby="runs-title">
            <header class="section-heading">
              <div>
                <h2 id="runs-title">Execution preview</h2>
                <p>See what is running, waiting or finished.</p>
              </div>
              <button
                type="button"
                class="text-button"
                (click)="navigate.emit('runs')"
              >
                Open runs<weave-icon name="chevron" />
              </button>
            </header>
            @if (loading()) {
              <p class="empty">Loading your execution preview…</p>
            } @else if (runs().length) {
              <ul class="preview-list">
                @for (run of runs().slice(0, 3); track run["id"]) {
                  <li>
                    <button
                      type="button"
                      class="preview-row"
                      (click)="navigate.emit('runs')"
                    >
                      <span class="row-icon"><weave-icon name="runs" /></span>
                      <span class="row-copy"
                        ><strong>{{
                          run["business_key"] || shortId(run["id"])
                        }}</strong
                        ><span>{{ runStatus(run) }}</span></span
                      ><weave-icon name="chevron" />
                    </button>
                  </li>
                }
              </ul>
            } @else {
              <div class="empty">
                <weave-icon name="runs" />
                <p>No executions in this preview.</p>
                <span
                  >After activation, start a run to follow its progress
                  here.</span
                >
              </div>
            }
          </section>
        </div>
      } @else {
        <section class="offline-guide" aria-labelledby="offline-title">
          <div>
            <h2 id="offline-title">A good place to begin</h2>
            <p>
              You can author and validate workflows locally. Connect Studio to a
              platform when you are ready to publish, run a process or work
              through a human inbox.
            </p>
          </div>
          <ol>
            <li>
              <strong>Shape the process</strong
              ><span>Create a draft or import an existing definition.</span>
            </li>
            <li>
              <strong>Check the definition</strong
              ><span
                >Use the canvas and source together, then validate your
                changes.</span
              >
            </li>
            <li>
              <strong>Connect when ready</strong
              ><span
                >Open Settings to import a login configuration, sign in, and
                choose an authorized workspace.</span
              >
            </li>
          </ol>
        </section>
      }
    </section>
  `,
})
export class HomeDashboard {
  connected = input(false);
  workspaceName = input("");
  tasks = input<Record<string, unknown>[]>([]);
  runs = input<Record<string, unknown>[]>([]);
  loading = input(false);
  createWorkflow = output<void>();
  importWorkflow = output<File>();
  navigate = output<"designer" | "tasks" | "runs">();
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
      this.importError = "Choose a .yaml, .yml or .json workflow definition.";
      return;
    }
    if (file.size > 1024 * 1024) {
      this.importError =
        "This file exceeds 1 MiB. Choose a smaller workflow definition.";
      return;
    }
    this.importError = "";
    this.importWorkflow.emit(file);
  }
  shortId(value: unknown) {
    return typeof value === "string" ? "Run " + value.slice(0, 8) : "Execution";
  }
  runStatus(run: Record<string, unknown>) {
    const state = run["state"] as Record<string, unknown> | undefined;
    if (state?.["manual_paused"]) return "Paused";
    const status = state?.["status"];
    return typeof status === "string"
      ? status.replaceAll("_", " ")
      : "Status unavailable";
  }
}
