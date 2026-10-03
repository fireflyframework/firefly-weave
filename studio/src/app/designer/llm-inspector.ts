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
  DoCheck,
  inject,
} from "@angular/core";
import type { App } from "../app";
import { ExpressionEditor } from "../property-grid";
import { TaskForm } from "../task-form";
import { SchemaDesigner } from "../forms/ui/schema-designer";
import { referencesAt } from "../forms/core/reference-context";
import { describeError } from "../errors";
import type { Schema } from "../task-schema";
import { validVersion } from "../forms/core/identifiers";
import type { Step } from "../model";

type RecordValue = Record<string, unknown>;
const object = (value: unknown): RecordValue =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as RecordValue)
    : {};

@Component({
  selector: "weave-llm-inspector",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [ExpressionEditor, TaskForm, SchemaDesigner],
  template: `
    <fieldset
      [disabled]="host.model.readonly || host.editingLocked"
      class="ai-inspector"
    >
      <h3>AI task</h3>
      <p class="hint">
        A durable worker runs this task using the selected workflow profile and
        authorized connection.
      </p>
      <label data-field="uses"
        >AI action version
        <input
          aria-label="AI action version"
          [value]="step['uses']"
          (input)="textField('uses', $event)"
      /></label>
      @if (host.profile && host.catalogState === "ready" && !installed) {
        <p class="hint">
          The selected action is not published in this project. Publish the
          Agentic worker action before activating this workflow.
        </p>
      }
      <label data-field="profile"
        >Workflow AI profile
        <select
          aria-label="Workflow AI profile"
          [value]="step['profile']"
          (change)="selectProfile($event)"
        >
          <option value="">Choose a profile…</option>
          @for (name of profileNames; track name) {
            <option [value]="name">{{ name }}</option>
          }
        </select></label
      >
      @if (!profileNames.includes(String(step["profile"]))) {
        <p class="hint">
          Configure a profile below to choose its provider, model and result
          fields.
        </p>
      }
      <label data-field="connection"
        >AI connection slot
        <select
          aria-label="AI connection slot"
          [value]="step['connection']"
          (change)="edit('connection', value($event))"
        >
          <option value="">Choose a slot…</option>
          @for (slot of host.workflowSlots; track slot.name) {
            <option [value]="slot.name">
              {{ slot.name }} · {{ slot.connector }}
            </option>
          }
        </select></label
      >
      <button type="button" (click)="host.openWorkflowSettings()">
        Manage workflow connections
      </button>
      <div data-field="prompt">
        <weave-expression-editor
          [value]="step['prompt']"
          label="AI prompt"
          heading="Prompt"
          [references]="promptReferences"
          (valueChange)="edit('prompt', $event)"
          (validityChange)="validity('prompt', $event)"
        />
      </div>
      <div data-field="context">
        <weave-expression-editor
          [value]="step['context']"
          label="AI context"
          heading="Context"
          [references]="contextReferences"
          (valueChange)="edit('context', $event)"
          (validityChange)="validity('context', $event)"
        />
      </div>
      <p class="hint">Typed result: /steps/{{ step.id }}/output/result</p>
      <details [open]="!profileNames.length">
        <summary>Configure workflow AI profiles</summary>
        <p class="hint">
          All steps that choose a profile share its model, reasoning and
          generation limits.
        </p>
        <label
          >Profile to edit
          <select
            aria-label="Profile to edit"
            [value]="profileName"
            (change)="loadProfile(value($event))"
          >
            <option value="">New profile…</option>
            @for (name of profileNames; track name) {
              <option [value]="name">{{ name }}</option>
            }
          </select></label
        >
        <button type="button" (click)="newProfile()">Add AI profile</button>
        <label data-field="spec/llmProfiles"
          >Profile name
          <input
            aria-label="AI profile name"
            [value]="profileName"
            [readOnly]="existingProfile"
            (input)="profileName = value($event); saveProfile()"
        /></label>
        @if (schema) {
          <div data-field="spec/llmProfiles">
            <weave-task-form
              [schema]="schema"
              [initialData]="initialProfile"
              (validityChange)="profileValidity($event)"
              (dataChange)="profileData($event)"
            />
          </div>
          <weave-schema-designer
            [schema]="resultSchema"
            heading="AI result fields"
            [preview]="false"
            (schemaChange)="resultSchema = $event; saveProfile()"
            (validityChange)="
              resultValid = $event; profileValidity(profileValid)
            "
          />
          @if (!profileValid || !profileName) {
            <p class="hint">
              Complete the required profile fields to save this profile.
            </p>
          }
          @if (profileValid && existingProfile) {
            <p role="status">
              Valid profile changes update the workflow automatically.
            </p>
          }
        } @else if (loading) {
          <p role="status">Loading profile fields…</p>
        } @else {
          <button type="button" (click)="loadSchema()">
            Load profile fields
          </button>
        }
      </details>
      @if (error) {
        <p class="field-error" role="alert">{{ error }}</p>
      }
    </fieldset>
  `,
  styles: [
    `
      :host {
        display: block;
        min-width: 0;
      }
      .ai-inspector {
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
        margin: 8px 0;
      }
      input,
      select {
        min-width: 0;
        max-width: 100%;
      }
      details[open] > summary {
        margin-bottom: 12px;
      }
    `,
  ],
})
export class LlmInspector implements OnInit, DoCheck, OnDestroy {
  @Input({ required: true }) host!: App;
  readonly String = String;
  private cdr = inject(ChangeDetectorRef);
  private alive = true;
  private selected = "";
  schema: Schema | null = null;
  loading = false;
  error = "";
  profileName = "";
  existingProfile = false;
  initialProfile: RecordValue = {};
  profile: RecordValue = {};
  resultSchema: RecordValue = { type: "object", properties: {} };
  profileValid = false;
  resultValid = true;
  private invalid = new Set<string>();
  get step() {
    return JSON.parse(this.host.inspectorBuffer) as Step;
  }
  get profiles() {
    return object(this.host.model.definition.spec["llmProfiles"]);
  }
  get profileNames() {
    return Object.keys(this.profiles);
  }
  get installed() {
    return this.host.actionVersions.some(
      (item) => `${item["name"]}@${item["version"]}` === this.step["uses"],
    );
  }
  get promptReferences() {
    return referencesAt(this.host.referenceContext, "/prompt");
  }
  get contextReferences() {
    return referencesAt(this.host.referenceContext, "/context");
  }
  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  ngOnInit() {
    void this.loadSchema();
    void this.host.loadActionCatalog();
  }
  ngDoCheck() {
    if (this.step?.id !== this.selected) {
      this.selected = this.step?.id ?? "";
      this.loadProfile(String(this.step?.["profile"] ?? ""));
    }
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
  textField(key: string, event: Event) {
    const text = this.value(event);
    const [name, version, extra] = text.split("@");
    const valid =
      /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(name ?? "") &&
      validVersion(version ?? "") &&
      extra === undefined;
    this.validity(key, valid);
    if (valid) this.edit(key, text);
  }
  selectProfile(event: Event) {
    const name = this.value(event);
    if (!name) return;
    this.edit("profile", name);
    this.loadProfile(name);
  }
  loadProfile(name: string) {
    this.profileName = name;
    this.existingProfile = !!this.profiles[name];
    const loaded = object(this.profiles[name]);
    this.resultSchema = object(loaded["outputSchema"]);
    if (!Object.keys(this.resultSchema).length)
      this.resultSchema = { type: "object", properties: {} };
    const { outputSchema: _, ...rest } = loaded;
    this.initialProfile = structuredClone(rest);
    this.profile = structuredClone(rest);
    this.profileValid = false;
  }
  newProfile() {
    let name = "default";
    let i = 1;
    while (this.profiles[name]) name = `profile-${i++}`;
    this.loadProfile(name);
  }
  profileData(value: RecordValue) {
    this.profile = value;
    this.saveProfile();
  }
  profileValidity(valid: boolean) {
    this.profileValid = valid;
    if (!valid || !this.resultValid)
      this.host.inspectorFieldValidity({
        path: "spec/llmProfiles",
        valid: false,
      });
  }
  saveProfile() {
    if (
      !this.profileValid ||
      !this.resultValid ||
      !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(this.profileName)
    )
      return;
    const document = structuredClone(this.host.model.definition);
    document.spec["llmProfiles"] = {
      ...this.profiles,
      [this.profileName]: { ...this.profile, outputSchema: this.resultSchema },
    };
    this.host.workflowEdit({ value: document, path: "spec/llmProfiles" });
    this.existingProfile = true;
  }
  async loadSchema() {
    this.loading = true;
    this.error = "";
    try {
      const schema = await this.host.api.request<RecordValue>(
        "/studio/contracts/llm-profile",
      );
      if (!this.alive) return;
      const properties = { ...object(schema["properties"]) };
      delete properties["outputSchema"];
      this.schema = {
        ...schema,
        properties,
        required: (schema["required"] as string[]).filter(
          (key) => key !== "outputSchema",
        ),
      } as Schema;
    } catch (error) {
      this.error = describeError(error).message;
    } finally {
      this.loading = false;
      this.cdr.markForCheck();
    }
  }
}
