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
  Output,
  EventEmitter,
  inject,
} from "@angular/core";
import { stringify } from "yaml";
import type { App } from "../app";
import { ExpressionEditor, PropertyValue } from "../property-grid";
import { TaskForm } from "../task-form";
import { SchemaDesigner } from "../forms/ui/schema-designer";
import { visibleRefs } from "../forms/core/scope";
import { ConditionEditor } from "./condition-editor";
import { describeDiagnostic } from "./diagnostic-copy";
import { describeError } from "../errors";
import { exportFile } from "../export-file";

type RecordValue = Record<string, unknown>;
export interface DecisionRuleDraft {
  id: string;
  when?: unknown;
  output: unknown;
}
export interface DecisionTableDraft {
  apiVersion: string;
  kind: "DecisionTable";
  metadata: { name: string; version: string };
  spec: {
    inputSchema: RecordValue;
    outputSchema: RecordValue;
    hitPolicy: string;
    rules: DecisionRuleDraft[];
    defaultOutput?: unknown;
  };
}
export const freshDecisionTable = (): DecisionTableDraft => ({
  apiVersion: "weave/v1alpha1",
  kind: "DecisionTable",
  metadata: { name: "payment-policy", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object", properties: {} },
    outputSchema: { type: "object", properties: {} },
    hitPolicy: "first",
    rules: [{ id: "rule-1", output: { literal: {} } }],
  },
});

@Component({
  selector: "weave-decision-table-editor",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    ExpressionEditor,
    PropertyValue,
    ConditionEditor,
    SchemaDesigner,
    TaskForm,
  ],
  template: `
    <section aria-label="Decision table editor" class="table-editor">
      <h3>Create a reusable decision table</h3>
      <p class="hint">
        Rules run in the order shown. Publish a version to reuse it in
        workflows.
      </p>
      <label
        >Table name
        <input
          aria-label="Table name"
          [value]="document.metadata.name"
          (input)="metadata('name', $event)"
      /></label>
      <label
        >Table version
        <input
          aria-label="New table version"
          [value]="document.metadata.version"
          (input)="metadata('version', $event)"
      /></label>
      <label
        >Matching policy
        <select
          aria-label="Matching policy"
          [value]="document.spec.hitPolicy"
          (change)="policy($event)"
        >
          <option value="first">First matching rule</option>
          <option value="unique">Exactly one matching rule</option>
          <option value="collect">Collect all matching rules</option>
        </select></label
      >
      <p class="hint">{{ policyHint }}</p>
      <details>
        <summary>Input fields</summary>
        <weave-schema-designer
          [schema]="document.spec.inputSchema"
          heading="Table input fields"
          [preview]="false"
          (schemaChange)="schema('inputSchema', $event)"
        />
      </details>
      <details>
        <summary>Result fields</summary>
        <label
          >Result type
          <select
            aria-label="Rule result type"
            [value]="rowSchema['type'] || 'object'"
            (change)="resultType($event)"
          >
            <option value="object">Fields</option>
            <option value="string">Text</option>
            <option value="number">Number</option>
            <option value="boolean">Yes or no</option>
          </select></label
        >
        @if (rowSchema["type"] === "object") {
          <weave-schema-designer
            [schema]="rowSchema"
            heading="Rule result fields"
            [preview]="false"
            (schemaChange)="resultSchema($event)"
          />
        }
      </details>
      <ol class="rules" aria-label="Decision rules">
        @for (rule of document.spec.rules; track $index; let i = $index) {
          <li class="rule" [attr.aria-label]="'Rule ' + (i + 1)">
            <div class="rule-heading">
              <strong>Rule {{ i + 1 }}</strong>
              <div class="rule-actions">
                <button
                  type="button"
                  [disabled]="i === 0"
                  [attr.aria-label]="'Move rule ' + (i + 1) + ' up'"
                  (click)="move(i, -1)"
                >
                  ↑
                </button>
                <button
                  type="button"
                  [disabled]="i === document.spec.rules.length - 1"
                  [attr.aria-label]="'Move rule ' + (i + 1) + ' down'"
                  (click)="move(i, 1)"
                >
                  ↓
                </button>
                <button
                  type="button"
                  [disabled]="document.spec.rules.length === 1"
                  [attr.aria-label]="'Remove rule ' + (i + 1)"
                  (click)="remove(i)"
                >
                  Remove
                </button>
              </div>
            </div>
            <label
              >Rule name
              <input
                [attr.aria-label]="'Rule ' + (i + 1) + ' name'"
                [value]="rule.id"
                (input)="ruleField(i, 'id', value($event))"
            /></label>
            <label class="check"
              ><input
                type="checkbox"
                [checked]="always(rule.when)"
                (change)="setAlways(i, $event)"
              />Always match</label
            >
            @if (!always(rule.when)) {
              @if (formulas.has(i)) {
                <weave-expression-editor
                  [value]="rule.when"
                  [label]="'Rule ' + (i + 1) + ' condition'"
                  [references]="references"
                  (valueChange)="ruleField(i, 'when', $event)"
                />
              } @else {
                <weave-condition-editor
                  [value]="rule.when"
                  [label]="'Rule ' + (i + 1) + ' condition'"
                  [references]="references"
                  (valueChange)="ruleField(i, 'when', $event)"
                  (formula)="formulas.add(i)"
                />
              }
            }
            <weave-expression-editor
              [value]="rule.output"
              [label]="'Rule ' + (i + 1) + ' result'"
              [heading]="'Rule ' + (i + 1) + ' result'"
              [references]="references"
              (valueChange)="ruleField(i, 'output', $event)"
            />
          </li>
        }
      </ol>
      <button
        type="button"
        [disabled]="document.spec.rules.length >= 1000"
        (click)="add()"
      >
        Add decision rule
      </button>
      @if (document.spec.hitPolicy !== "collect") {
        <label class="check"
          ><input
            type="checkbox"
            [checked]="document.spec.defaultOutput !== undefined"
            (change)="toggleDefault($event)"
          />Provide a result when no rules match</label
        >
        @if (document.spec.defaultOutput !== undefined) {
          <weave-expression-editor
            [value]="document.spec.defaultOutput"
            label="Default result"
            heading="Default result"
            [references]="references"
            (valueChange)="specField('defaultOutput', $event)"
          />
        }
      }
      <details>
        <summary>Try an input</summary>
        <weave-task-form
          [schema]="document.spec.inputSchema"
          [initialData]="testInput"
          (dataChange)="testInput = $event"
          (validityChange)="testValid = $event"
        />
        <button
          type="button"
          [disabled]="
            busy || !testValid || !host.profile || !host.can('compile')
          "
          (click)="evaluate()"
        >
          Evaluate rules
        </button>
        @if (!host.profile) {
          <p class="hint">Connect to a platform to evaluate this table.</p>
        }
        @if (testResult) {
          <p>Matched rules: {{ testResult["matched_rule_ids"] }}</p>
          <weave-property-value
            [value]="testResult['output']"
            [readOnly]="true"
          />
        }
      </details>
      @if (issues.length) {
        <ul role="alert">
          @for (issue of issues; track $index) {
            <li>{{ issue }}</li>
          }
        </ul>
      }
      @if (notice) {
        <p role="status">{{ notice }}</p>
      }
      <div class="editor-actions">
        <button type="button" [disabled]="busy" (click)="validate()">
          Check table
        </button>
        <button type="button" [disabled]="busy" (click)="download()">
          Export table YAML
        </button>
        <button
          type="button"
          class="primary"
          [disabled]="busy || !host.profile || !host.can('definition.publish')"
          (click)="publish()"
        >
          Publish and use table
        </button>
      </div>
    </section>
  `,
  styles: [
    `
      :host {
        display: block;
        min-width: 0;
      }
      .table-editor {
        display: grid;
        gap: 12px;
      }
      .table-editor label {
        display: grid;
        gap: 6px;
      }
      .table-editor .check {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .rules {
        list-style: none;
        padding: 0;
        display: grid;
        gap: 12px;
      }
      .rule {
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 12px;
        display: grid;
        gap: 12px;
        min-width: 0;
      }
      .rule-heading,
      .rule-actions,
      .editor-actions {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        align-items: center;
      }
      .rule-heading {
        justify-content: space-between;
      }
      input,
      select {
        max-width: 100%;
        min-width: 0;
      }
      details[open] > summary {
        margin-bottom: 12px;
      }
    `,
  ],
})
export class DecisionTableEditor {
  @Input({ required: true }) host!: App;
  @Input() document = freshDecisionTable();
  @Output() documentChange = new EventEmitter<DecisionTableDraft>();
  @Output() use = new EventEmitter<string>();
  private cdr = inject(ChangeDetectorRef);
  formulas = new Set<number>();
  busy = false;
  issues: string[] = [];
  notice = "";
  testInput: RecordValue = {};
  testValid = true;
  testResult: RecordValue | null = null;
  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  get policyHint() {
    return this.document.spec.hitPolicy === "collect"
      ? "All matching results become a list. No matches returns an empty list."
      : this.document.spec.hitPolicy === "unique"
        ? "The run stops for review if more than one rule matches."
        : "The first match wins. Put more specific rules before broader rules.";
  }
  get rowSchema() {
    return this.document.spec.hitPolicy === "collect"
      ? (this.document.spec.outputSchema["items"] as RecordValue)
      : this.document.spec.outputSchema;
  }
  get references() {
    return visibleRefs(
      {
        spec: {
          inputSchema: this.document.spec.inputSchema,
          steps: [],
          output: { literal: null },
        },
      },
      "$workflow",
      "/spec/output",
    );
  }
  changed() {
    this.documentChange.emit(structuredClone(this.document));
    this.issues = [];
    this.notice = "";
    this.testResult = null;
  }
  metadata(key: "name" | "version", event: Event) {
    this.document.metadata[key] = this.value(event);
    this.changed();
  }
  schema(key: "inputSchema" | "outputSchema", value: RecordValue) {
    this.document.spec[key] = value;
    this.changed();
  }
  specField(key: "defaultOutput", value: unknown) {
    this.document.spec[key] = value;
    this.changed();
  }
  policy(event: Event) {
    const before = this.document.spec.hitPolicy;
    const next = this.value(event);
    if (before === next) return;
    const schema = this.rowSchema;
    this.document.spec.hitPolicy = next;
    this.document.spec.outputSchema =
      next === "collect" ? { type: "array", items: schema } : schema;
    if (next === "collect") delete this.document.spec.defaultOutput;
    this.changed();
  }
  resultType(event: Event) {
    this.resultSchema({
      type: this.value(event),
      ...(this.value(event) === "object" ? { properties: {} } : {}),
    });
  }
  resultSchema(value: RecordValue) {
    this.document.spec.outputSchema =
      this.document.spec.hitPolicy === "collect"
        ? { ...this.document.spec.outputSchema, items: value }
        : value;
    this.changed();
  }
  always(value: unknown) {
    return (
      !!value &&
      typeof value === "object" &&
      (value as RecordValue)["literal"] === true
    );
  }
  setAlways(index: number, event: Event) {
    this.ruleField(
      index,
      "when",
      (event.target as HTMLInputElement).checked
        ? { literal: true }
        : undefined,
    );
  }
  ruleField(index: number, key: "id" | "when" | "output", value: unknown) {
    this.document.spec.rules[index] = {
      ...this.document.spec.rules[index],
      [key]: value,
    };
    this.changed();
  }
  add() {
    let index = this.document.spec.rules.length + 1;
    while (this.document.spec.rules.some((rule) => rule.id === `rule-${index}`))
      index++;
    this.document.spec.rules.push({
      id: `rule-${index}`,
      output: { literal: {} },
    });
    this.changed();
  }
  remove(index: number) {
    this.document.spec.rules.splice(index, 1);
    this.formulas.clear();
    this.changed();
  }
  move(index: number, direction: number) {
    const rules = this.document.spec.rules;
    [rules[index], rules[index + direction]] = [
      rules[index + direction],
      rules[index],
    ];
    this.formulas.clear();
    this.changed();
  }
  toggleDefault(event: Event) {
    if ((event.target as HTMLInputElement).checked)
      this.document.spec.defaultOutput = { literal: {} };
    else delete this.document.spec.defaultOutput;
    this.changed();
  }
  async validate() {
    this.busy = true;
    this.issues = [];
    try {
      const result = await this.host.api.validate(
        stringify(this.document),
        "yaml",
      );
      this.issues = result.diagnostics
        .filter((item) => item.severity === "error")
        .map((item) => describeDiagnostic(item).text);
      this.notice = result.validationOk ? "Table checks passed." : "";
      return result.validationOk;
    } catch (error) {
      this.issues = [describeError(error).message];
      return false;
    } finally {
      this.busy = false;
      this.cdr.markForCheck();
    }
  }
  download() {
    exportFile(
      `${this.document.metadata.name}.yaml`,
      "text/yaml",
      stringify(this.document),
    );
    this.notice = "Table YAML exported.";
  }
  async publish() {
    if (!(await this.validate())) return;
    this.busy = true;
    try {
      await this.host.api.mutate(
        `${this.host.api.project}/decision-tables`,
        "POST",
        { source: stringify(this.document), format: "yaml" },
      );
      this.use.emit(
        `${this.document.metadata.name}@${this.document.metadata.version}`,
      );
    } catch (error) {
      this.issues = [describeError(error).message];
    } finally {
      this.busy = false;
      this.cdr.markForCheck();
    }
  }
  async evaluate() {
    this.busy = true;
    this.issues = [];
    try {
      this.testResult = await this.host.api.request<RecordValue>(
        `${this.host.api.project}/compiler/evaluate-decision`,
        "POST",
        {
          source: stringify(this.document),
          format: "yaml",
          input: this.testInput,
        },
      );
    } catch (error) {
      this.issues = [describeError(error).message];
    } finally {
      this.busy = false;
      this.cdr.markForCheck();
    }
  }
}
