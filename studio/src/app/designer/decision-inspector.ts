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
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  Input,
  OnInit,
  OnDestroy,
  inject,
} from "@angular/core";
import type { App } from "../app";
import { ExpressionEditor } from "../property-grid";
import { referencesAt } from "../forms/core/reference-context";
import {
  DecisionTableEditor,
  freshDecisionTable,
} from "./decision-table-editor";
import { describeError } from "../errors";
import { validVersion } from "../forms/core/identifiers";
import type { Step } from "../model";

@Component({
  selector: "weave-decision-inspector",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [ExpressionEditor, DecisionTableEditor],
  template: `
    <fieldset
      [disabled]="host.model.readonly || host.editingLocked"
      class="decision-inspector"
    >
      <h3>Reusable decision table</h3>
      <p class="hint">
        The workflow uses this exact published version. Its rules run without a
        worker.
      </p>
      @if (host.profile && host.can("catalog.read")) {
        <label
          >Published table
          <select
            aria-label="Published table"
            [value]="step['uses']"
            (change)="choose(value($event))"
          >
            <option value="">Choose a table…</option>
            @for (table of tables; track table["id"]) {
              <option [value]="table['name'] + '@' + table['version']">
                {{ table["name"] }} · {{ table["version"] }}
              </option>
            }
          </select></label
        >
        <button type="button" [disabled]="loading" (click)="load()">
          {{ loading ? "Loading tables…" : "Refresh tables" }}
        </button>
        @if (nextCursor) {
          <button type="button" [disabled]="loading" (click)="load(true)">
            Load more tables
          </button>
        }
      }
      <label data-field="uses"
        >Table version
        <input
          aria-label="Table version"
          placeholder="payment-policy@1.0.0"
          [attr.aria-invalid]="!!referenceError || null"
          [value]="step['uses']"
          (input)="reference($event)"
          (blur)="reference($event)"
      /></label>
      @if (referenceError) {
        <p class="field-error" role="alert">{{ referenceError }}</p>
      }
      @if (error) {
        <p class="field-error" role="alert">{{ error }}</p>
      }
      <div data-field="with">
        <weave-expression-editor
          [value]="step['with']"
          [expectedSchema]="inputSchema"
          label="Table input"
          heading="Table input"
          [references]="references"
          (valueChange)="edit('with', $event)"
          (validityChange)="validity('with', $event)"
        />
      </div>
      <p class="hint">Result: /steps/{{ step.id }}/output</p>
      <button type="button" (click)="creating = !creating">
        {{ creating ? "Back to step settings" : "Create a decision table" }}
      </button>
      @if (creating) {
        <weave-decision-table-editor
          [host]="host"
          [document]="draft"
          (documentChange)="draft = $event"
          (use)="published($event)"
        />
      }
    </fieldset>
  `,
  styles: [
    `
      :host {
        display: block;
        min-width: 0;
      }
      .decision-inspector {
        border: 0;
        padding: 0;
        margin: 0;
        display: grid;
        gap: 12px;
        min-width: 0;
      }
      label {
        display: grid;
        gap: 6px;
      }
      input,
      select {
        min-width: 0;
        max-width: 100%;
      }
    `,
  ],
})
export class DecisionInspector implements OnInit, OnDestroy {
  @Input({ required: true }) host!: App;
  private cdr = inject(ChangeDetectorRef);
  private alive = true;
  tables: Record<string, unknown>[] = [];
  nextCursor: string | null = null;
  loading = false;
  error = "";
  referenceError = "";
  creating = false;
  draft = freshDecisionTable();
  private invalid = new Set<string>();
  get step() {
    return JSON.parse(this.host.inspectorBuffer) as Step;
  }
  get references() {
    return referencesAt(this.host.referenceContext, "/with");
  }
  get inputSchema(): Record<string, unknown> | null {
    const contract = this.host.decisionContracts.get(
      String(this.step["uses"] ?? ""),
    );
    const spec = contract?.["spec"] as Record<string, unknown> | undefined;
    const schema = spec?.["inputSchema"];
    return schema && typeof schema === "object" && !Array.isArray(schema)
      ? (schema as Record<string, unknown>)
      : null;
  }
  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  ngOnInit() {
    void this.load();
  }
  ngOnDestroy() {
    this.alive = false;
  }
  edit(key: string, value: unknown) {
    this.host.stepEdit({ ...this.step, [key]: value }, key);
  }
  validity(path: string, valid: boolean) {
    if (valid) this.invalid.delete(path);
    else this.invalid.add(path);
    this.host.propertyValid.set(this.invalid.size === 0);
    this.host.inspectorFieldValidity({ path, valid });
  }
  reference(event: Event) {
    const value = this.value(event);
    const [name, version, extra] = value.split("@");
    const valid =
      /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(name ?? "") &&
      validVersion(version ?? "") &&
      extra === undefined;
    this.referenceError = valid
      ? ""
      : "Enter a table name and version, such as payment-policy@1.0.0.";
    this.validity("uses", valid);
    if (valid) this.edit("uses", value);
  }
  async load(append = false) {
    if (!this.host.profile || !this.host.can("catalog.read")) return;
    this.loading = true;
    this.error = "";
    const profile = this.host.profile;
    try {
      const page = await this.host.api.page(
        "decision-tables",
        false,
        append ? (this.nextCursor ?? undefined) : undefined,
      );
      if (!this.alive || this.host.profile !== profile) return;
      this.tables = append ? [...this.tables, ...page.items] : page.items;
      this.nextCursor = page.next_cursor;
      await this.contract(String(this.step["uses"] ?? ""));
    } catch (error) {
      this.error = describeError(error).message;
    } finally {
      this.loading = false;
      this.cdr.markForCheck();
    }
  }
  async choose(uses: string) {
    if (!uses) return;
    this.edit("uses", uses);
    await this.contract(uses);
  }
  async contract(uses: string) {
    const table = this.tables.find(
      (item) => `${item["name"]}@${item["version"]}` === uses,
    );
    if (!table) return;
    const profile = this.host.profile;
    try {
      const detail = await this.host.api.request<Record<string, unknown>>(
        `${this.host.api.project}/decision-tables/${encodeURIComponent(String(table["id"]))}/export`,
      );
      if (!this.alive || this.host.profile !== profile) return;
      const document = detail["definition"] ?? detail["document"];
      if (document && typeof document === "object")
        this.host.cacheDecisionContract(
          uses,
          document as Record<string, unknown>,
        );
    } catch (error) {
      this.error = describeError(error).message;
    } finally {
      this.cdr.markForCheck();
    }
  }
  published(uses: string) {
    this.edit("uses", uses);
    this.creating = false;
    void this.load();
  }
}
