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
// Tenant > project > environment picker. Native radios keep arrow-key
// selection; nested fieldsets give each environment its group context.
import { Component, computed, input, output, signal } from "@angular/core";
import {
  WorkspaceOption,
  filterWorkspaces,
  groupWorkspaces,
  workspaceKey,
} from "./connection";

let sequence = 0;

@Component({
  selector: "weave-workspace-picker",
  standalone: true,
  template: `
    @if (options().length > 8) {
      <label class="workspace-search" [for]="id + '-search'"
        >Search workspaces
        <input
          [id]="id + '-search'"
          type="search"
          autocomplete="off"
          spellcheck="false"
          placeholder="Tenant, project, or environment"
          [attr.aria-controls]="id + '-list'"
          [value]="query()"
          (input)="query.set(text($event))"
          (keydown.enter)="searchEnter($event)"
      /></label>
      <p class="sr-only" role="status">
        {{ shown().length }} of {{ options().length }} workspaces shown
      </p>
    }
    <div class="workspace-list" [id]="id + '-list'">
      @for (tenant of groups(); track tenant.tenant_id) {
        <fieldset class="tenant-group">
          <legend>{{ tenant.tenant_name }}</legend>
          @for (project of tenant.projects; track project.project_id) {
            <fieldset class="project-group">
              <legend>{{ project.project_name }}</legend>
              <div class="environment-grid">
                @for (
                  environment of project.environments;
                  track environment.environment_id
                ) {
                  <label
                    class="environment-card"
                    [class.selected]="key(environment) === selected()"
                  >
                    <input
                      type="radio"
                      [name]="id"
                      [value]="key(environment)"
                      [checked]="key(environment) === selected()"
                      [attr.aria-invalid]="invalid() ? 'true' : null"
                      [attr.aria-describedby]="describedBy() || null"
                      (change)="selectedChange.emit(environment)"
                    /><span class="sr-only"
                      >{{ tenant.tenant_name }} /
                      {{ project.project_name }} / </span
                    ><span class="environment-name">{{
                      environment.environment_name
                    }}</span>
                  </label>
                }
              </div>
            </fieldset>
          }
        </fieldset>
      } @empty {
        <p class="no-match">
          No workspace matches “{{ query() }}”. Try another name.
        </p>
      }
    </div>
    @if (truncated()) {
      <p class="hint">
        Your account can see more workspaces than Studio lists. If yours is
        missing, ask your administrator.
      </p>
    }
  `,
  styles: [
    `
      :host {
        display: block;
        min-width: 0;
      }
      .workspace-search {
        margin-bottom: 16px;
      }
      fieldset {
        border: 0;
        margin: 0;
        padding: 0;
        min-width: 0;
      }
      .tenant-group {
        border: 1px solid var(--line);
        border-radius: 10px;
        padding: 12px 16px 16px;
        margin-bottom: 12px;
      }
      .tenant-group > legend {
        font-weight: 700;
        font-size: 13px;
        padding: 0 6px;
        margin-left: -6px;
        overflow-wrap: anywhere;
      }
      .project-group {
        margin-top: 12px;
      }
      .project-group > legend {
        font-size: 12px;
        color: var(--muted);
        font-weight: 600;
        margin-bottom: 8px;
        overflow-wrap: anywhere;
      }
      .environment-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
        gap: 8px;
      }
      .environment-card {
        display: flex;
        align-items: center;
        gap: 10px;
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 10px 12px;
        font-size: 13px;
        font-weight: 600;
        cursor: pointer;
        min-width: 0;
        background: var(--surface);
      }
      .environment-card:hover {
        border-color: var(--border-hover);
        background: var(--hover);
      }
      .environment-card.selected {
        border-color: var(--accent);
        background: var(--selected);
        box-shadow: inset 0 0 0 1px var(--accent);
      }
      .environment-card input {
        width: 16px;
        height: 16px;
        margin: 0;
        flex: none;
        accent-color: var(--accent);
      }
      .environment-card:has(input:focus-visible) {
        outline: 2px solid var(--focus);
        outline-offset: 2px;
      }
      .environment-name {
        overflow-wrap: anywhere;
        min-width: 0;
      }
      .no-match {
        color: var(--muted);
        font-size: 13px;
      }
    `,
  ],
})
export class WorkspacePicker {
  options = input<WorkspaceOption[]>([]);
  selected = input("");
  truncated = input(false);
  invalid = input(false);
  describedBy = input("");
  selectedChange = output<WorkspaceOption>();
  query = signal("");
  id = `workspace-picker-${++sequence}`;
  shown = computed(() => filterWorkspaces(this.options(), this.query()));
  groups = computed(() => groupWorkspaces(this.shown()));
  key = workspaceKey;
  text(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  /**
   * Enter in the search never submits the surrounding form (that would start
   * working with a workspace the filter may hide); a single match is selected.
   */
  searchEnter(event: Event) {
    event.preventDefault();
    const shown = this.shown();
    if (shown.length === 1) this.selectedChange.emit(shown[0]);
  }
}
