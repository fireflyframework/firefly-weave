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
  Component,
  DoCheck,
  Input,
  OnDestroy,
  OnInit,
} from "@angular/core";
import type { App } from "../app";
import type { Step } from "../model";
import {
  ExpressionEditor,
  PropertyDraft,
  StepPropertyGrid,
} from "../property-grid";
import { SchemaDesigner } from "../forms/ui/schema-designer";
import { ReferenceCombobox } from "../forms/ui/reference-combobox";
import { referencesAt } from "../forms/core/reference-context";
import { readAssignmentBindings } from "../integrations/activation-requirements";

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

@Component({
  selector: "weave-human-inspector",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    ExpressionEditor,
    StepPropertyGrid,
    SchemaDesigner,
    ReferenceCombobox,
  ],
  template: `<fieldset
    class="human-inspector"
    [disabled]="host.model.readonly || host.editingLocked"
  >
    <section class="human-section">
      <h3>Who</h3>
      <label data-field="assignment"
        >Assignment binding
        <input
          aria-label="Assignment binding"
          [value]="assignment"
          (input)="setAssignment($event)"
          (blur)="touch('assignment')"
          list="human-assignment-options"
          [attr.aria-invalid]="shownError('assignment') ? true : null"
        />
      </label>
      <datalist id="human-assignment-options">
        @for (name of assignments; track name) {
          <option [value]="name"></option>
        }
      </datalist>
      <p class="hint">
        Choose the people or groups when activating this workflow. This name
        connects the task to that assignment.
      </p>
      @if (shownError("assignment")) {
        <p class="field-error" role="alert">{{ shownError("assignment") }}</p>
      }
    </section>
    <section class="human-section">
      <h3>What they see</h3>
      <div data-field="title">
        @if (titleReference) {
          <span class="property-label">Title</span>
          <div class="title-token">
            <span>{{ referenceLabel(titleReference) }}</span
            ><button
              type="button"
              aria-label="Remove title data"
              (click)="removeTitleData()"
            >
              ×
            </button>
          </div>
        } @else if (literalTitle) {
          <label
            >Title<input
              aria-label="Title"
              [value]="titleText"
              (input)="setTitle($event)"
              (blur)="touch('title')"
              [attr.aria-invalid]="shownError('title') ? true : null"
          /></label>
        } @else {
          <weave-expression-editor
            [value]="step['title']"
            heading="Title"
            label="Title"
            [references]="titleReferences"
            (valueChange)="edit('title', $event)"
            (validityChange)="validity('title', $event)"
          />
        }
        <button type="button" (click)="showTitleData = !showTitleData">
          Insert data
        </button>
        @if (showTitleData) {
          <weave-reference-combobox
            [value]="''"
            [options]="titleReferences"
            [target]="textSchema"
            ariaLabel="Title data"
            placeholder="Choose data…"
            (picked)="pickTitle($event.ref)"
          />
          <p class="hint">Use a text field as the task title.</p>
        }
        @if (shownError("title")) {
          <p class="field-error" role="alert">{{ shownError("title") }}</p>
        }
      </div>
      <div data-field="context">
        <weave-expression-editor
          [value]="step['context']"
          heading="Context"
          label="Context"
          [references]="contextReferences"
          (valueChange)="edit('context', $event)"
          (validityChange)="validity('context', $event)"
        />
      </div>
    </section>
    <section class="human-section">
      <h3>How they answer</h3>
      <div data-field="decisions">
        <span class="property-label">Answers</span>
        <div class="answer-chips">
          @for (answer of answers; track $index) {
            <div class="answer-chip">
              <input
                [attr.aria-label]="'Answer ' + ($index + 1)"
                [value]="answer"
                (input)="renameAnswer($index, $event)"
                (blur)="touch('decisions')"
                [attr.aria-invalid]="shownError('decisions') ? true : null"
              />
              <button
                type="button"
                [attr.aria-label]="'Remove answer ' + ($index + 1)"
                [disabled]="answers.length === 1"
                (click)="removeAnswer($index)"
              >
                ×
              </button>
            </div>
          }
        </div>
        @if (shownError("decisions")) {
          <p class="field-error" role="alert">{{ shownError("decisions") }}</p>
        }
        <div class="add-answer">
          <input
            #newAnswer
            aria-label="New answer"
            placeholder="New answer"
            (keydown.enter)="$event.preventDefault(); addAnswer(newAnswer)"
          /><button
            type="button"
            [disabled]="answers.length >= 32"
            (click)="addAnswer(newAnswer)"
          >
            Add answer
          </button>
        </div>
        @if (answerError) {
          <p class="field-error" role="alert">{{ answerError }}</p>
        }
        <p class="hint">
          Use unique names. Renaming an answer updates its decision paths.
        </p>
      </div>
      <div data-field="formSchema">
        <weave-schema-designer
          heading="Form schema"
          purpose="form"
          [schema]="step['formSchema']"
          [readonly]="host.model.readonly || host.editingLocked"
          [inferSchema]="host.inferSchema"
          (schemaChange)="edit('formSchema', $event)"
          (validityChange)="validity('formSchema', $event)"
        />
      </div>
      <button
        type="button"
        [disabled]="!!errors['decisions']"
        (click)="host.branchOnDecision()"
      >
        Create a path for each answer
      </button>
    </section>
    <section class="human-section">
      <h3>Deadlines</h3>
      <weave-step-property-grid
        [step]="host.propertyStep!"
        [readOnly]="host.model.readonly || host.editingLocked"
        [hiddenFields]="deadlineHidden"
        (fieldChange)="edit($event.path, $any($event.value)[$event.path])"
        (fieldValidity)="validity($event.path, $event.valid)"
      />
      <p class="hint">
        Due after: marks the task as overdue, but people can still answer.
      </p>
      <p class="hint">
        Expires after: the task expires and can no longer be answered; the
        workflow receives a timeout.
      </p>
    </section>
  </fieldset>`,
  styles: [
    `
      :host {
        display: block;
        min-width: 0;
      }
      .human-inspector {
        border: 0;
        margin: 0;
        padding: 0;
        min-width: 0;
      }
      .human-section {
        display: grid;
        gap: 12px;
        padding: 0 0 20px;
        margin: 0 0 20px;
        border-bottom: 1px solid var(--line);
      }
      .human-section:last-child {
        border-bottom: 0;
        margin-bottom: 0;
        padding-bottom: 0;
      }
      .human-section h3 {
        margin: 0;
      }
      .human-section label {
        display: grid;
        gap: 6px;
      }
      .human-section input {
        min-width: 0;
        width: 100%;
        box-sizing: border-box;
      }
      .human-section [data-field] {
        min-width: 0;
      }
      .answer-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin: 8px 0;
      }
      .answer-chip,
      .title-token {
        display: flex;
        align-items: center;
        gap: 4px;
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 3px;
        max-width: 100%;
        background: var(--surface);
      }
      .answer-chip {
        flex: 1 1 120px;
        min-width: 0;
      }
      .answer-chip input {
        border: 0;
        background: transparent;
      }
      .answer-chip button,
      .title-token button {
        flex: 0 0 auto;
      }
      .title-token span {
        overflow-wrap: anywhere;
        padding: 0 6px;
      }
      .add-answer {
        display: flex;
        gap: 8px;
        margin-top: 10px;
      }
      .add-answer input {
        flex: 1;
      }
      .add-answer button {
        flex: 0 0 auto;
      }
      .hint {
        margin: 0;
      }
    `,
  ],
})
export class HumanInspector implements DoCheck, OnInit, OnDestroy {
  @Input({ required: true }) host!: App;
  private alive = true;
  private previous: unknown;
  private source = "";
  private current!: Step;
  private invalid = new Set<string>();
  touched = new Set<string>();
  errors: Record<string, string> = {};
  assignment = "";
  assignments: string[] = [];
  answers: string[] = [];
  answerError = "";
  titleText = "";
  showTitleData = false;
  textSchema = { type: "string" };
  deadlineHidden = [
    "assignment",
    "title",
    "context",
    "decisions",
    "formSchema",
  ];
  get step(): Step {
    if (this.source !== this.host.inspectorBuffer) {
      this.source = this.host.inspectorBuffer;
      this.current = JSON.parse(this.source) as Step;
    }
    return this.current;
  }
  get literalTitle() {
    return Object.hasOwn(record(this.step["title"]), "literal");
  }
  get titleReference() {
    return String(record(this.step["title"])["ref"] ?? "");
  }
  get titleReferences() {
    return referencesAt(this.host.referenceContext, "/title").filter(
      (item) => item.schema?.["type"] === "string",
    );
  }
  get contextReferences() {
    return referencesAt(this.host.referenceContext, "/context");
  }
  ngDoCheck() {
    if (this.previous === this.host.propertyStep) return;
    this.previous = this.host.propertyStep;
    this.assignment = String(this.step["assignment"] ?? "");
    this.answers = Array.isArray(this.step["decisions"])
      ? [...(this.step["decisions"] as string[])]
      : ["approve", "reject"];
    this.titleText = String(record(this.step["title"])["literal"] ?? "");
    this.errors = {};
    this.invalid.clear();
    this.touched.clear();
  }
  async ngOnInit() {
    if (!this.host.profile || !this.host.can("assignment.read")) return;
    const profile = this.host.profile;
    try {
      const response = await this.host.api.request<{ items: unknown }>(
        `${this.host.api.environment}/human-assignments`,
      );
      if (this.alive && this.host.profile === profile) {
        this.assignments = [
          ...new Set(
            readAssignmentBindings(response.items)
              .filter((item) => item.enabled)
              .map((item) => item.name),
          ),
        ];
        this.host.refreshView();
      }
    } catch {
      /* Assignment suggestions do not prevent authoring a binding name. */
    }
  }
  ngOnDestroy() {
    this.alive = false;
  }
  text(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  touch(path: string) {
    this.touched.add(path);
  }
  shownError(path: string) {
    return this.touched.has(path) ? this.errors[path] : "";
  }
  validity(path: string, valid: boolean) {
    valid ? this.invalid.delete(path) : this.invalid.add(path);
    this.host.propertyValid.set(!this.invalid.size);
    this.host.inspectorFieldValidity({ path, valid });
  }
  edit(path: string, value: unknown) {
    const step = structuredClone(this.step);
    if (value === undefined) delete step[path];
    else step[path] = value;
    const draft = new PropertyDraft(step);
    const error = draft.errors.get(path) ?? "";
    this.errors[path] = error;
    if (error || path === "assignment" || path === "decisions")
      this.validity(path, !error);
    if (!error) this.host.stepEdit(step, path);
  }
  setAssignment(event: Event) {
    this.assignment = this.text(event);
    this.edit("assignment", this.assignment);
  }
  setTitle(event: Event) {
    this.titleText = this.text(event);
    this.validity(
      "title",
      this.titleText.length > 0 && this.titleText.length <= 512,
    );
    this.edit("title", { literal: this.titleText });
  }
  referenceLabel(ref: string) {
    return (
      this.titleReferences
        .find((item) => item.ref === ref)
        ?.breadcrumb.replace(/^Workflow input/, "Input") ?? ref
    );
  }
  pickTitle(ref: string) {
    this.validity("title", true);
    this.edit("title", { ref });
    this.showTitleData = false;
  }
  removeTitleData() {
    this.validity("title", true);
    this.edit("title", { literal: this.titleText || "Review request" });
  }
  renameAnswer(index: number, event: Event) {
    this.answers[index] = this.text(event);
    this.saveAnswers();
  }
  saveAnswers() {
    const problem = this.answers.some((answer) => !identifier.test(answer))
      ? "Use letters, numbers, dots, underscores or hyphens."
      : new Set(this.answers).size !== this.answers.length
        ? "Use a unique answer."
        : "";
    this.errors["decisions"] = problem;
    this.validity("decisions", !problem);
    if (!problem) this.edit("decisions", [...this.answers]);
  }
  addAnswer(input: HTMLInputElement) {
    const name = input.value.trim();
    this.answerError = !identifier.test(name)
      ? "Enter an answer using letters, numbers, dots, underscores or hyphens."
      : this.answers.includes(name)
        ? "Use a unique answer."
        : "";
    if (this.answerError || this.answers.length >= 32) return;
    this.answers.push(name);
    input.value = "";
    this.saveAnswers();
    input.focus();
  }
  removeAnswer(index: number) {
    if (this.answers.length <= 1) return;
    this.answers.splice(index, 1);
    this.saveAnswers();
  }
}
