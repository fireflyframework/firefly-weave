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
import { Modal } from "../dialog";
import { ExpressionEditor } from "../property-grid";
import { aiConnector } from "../integrations/ai-provider-connection";
import { compatibleSlots, slotName } from "../integrations/slot-binding";
import { Select } from "../forms/ui/select";
import { CatalogPicker } from "../integrations/catalog-picker";
import type { ActionPickerChoice } from "../integrations/action-picker";
import { AiProfileEditor } from "../integrations/ai-profile-editor";
import { AiSetupWizard } from "../integrations/ai-setup-wizard";
import { AiSharedContextPicker } from "../integrations/ai-shared-context-picker";
import { SchemaDesigner } from "../forms/ui/schema-designer";
import { referencesAt } from "../forms/core/reference-context";
import { describeError } from "../errors";
import type { Schema } from "../task-schema";
import { missingData } from "../forms/core/form-model";
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
  imports: [
    ExpressionEditor,
    CatalogPicker,
    Modal,
    AiProfileEditor,
    AiSetupWizard,
    AiSharedContextPicker,
    SchemaDesigner,
    Select,
  ],
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
      @if (host.profile && host.can("catalog.read")) {
        <weave-catalog-picker
          [host]="host"
          label="Published AI action"
          [canCreate]="false"
          [value]="String(step['uses'] ?? '')"
          [disabled]="
            host.model.readonly || host.editingLocked || actionLoading
          "
          (choose)="chooseAction($event)"
        />
        <p class="hint">Choose an action that runs the Agentic worker.</p>
        @if (actionLoading) {
          <p role="status">Checking the AI action…</p>
        }
        @if (host.catalogState === "error") {
          <p role="alert">{{ host.catalogError?.message }}</p>
          <button type="button" (click)="host.loadActionCatalog(false, true)">
            Retry action list
          </button>
        }
      } @else {
        <label data-field="uses"
          >AI action version
          <input
            aria-label="AI action version"
            [value]="step['uses']"
            [attr.aria-invalid]="invalid.has('uses') || null"
            (input)="textField('uses', $event)"
          />
        </label>
        @if (invalid.has("uses")) {
          <p class="field-error" role="alert">
            Enter an action name and version, such as
            weave-agentic-generate&#64;1.0.0.
          </p>
        }
      }
      @if (host.profile && host.catalogState === "ready" && !installed) {
        <p class="hint">
          The selected action is not in the loaded catalog. Load more actions or
          publish the Agentic worker action before activating this workflow.
        </p>
      }
      <div data-field="profile">
        <weave-select
          label="Workflow AI profile"
          [options]="profileOptions"
          [value]="String(step['profile'] ?? '')"
          [disabled]="host.model.readonly || host.editingLocked"
          (choose)="selectProfile($event)"
        />
      </div>
      @if (!profilesOpen) {
        <weave-select
          label="AI connection slot"
          [options]="slotOptions"
          [value]="String(step['connection'] ?? '')"
          [disabled]="host.model.readonly || host.editingLocked"
          (choose)="selectConnection($event)"
        />
        @if (!aiSlots.length) {
          <p class="hint">
            Configure a workflow AI profile to add a connection slot.
          </p>
        }
      }
      @if (!profileNames.includes(String(step["profile"]))) {
        <p class="hint">
          Configure a profile below to choose its provider, model and result
          fields.
        </p>
      }
      <p class="hint">
        Workflow profiles do not change Weave AI. A platform operator must
        install and authorize the Agentic worker before these tasks can run.
      </p>
      <div data-field="prompt">
        <weave-expression-editor
          [value]="step['prompt']"
          [expectedSchema]="{ type: 'string' }"
          label="AI prompt"
          heading="Prompt"
          [multiline]="true"
          [references]="promptReferences"
          (valueChange)="edit('prompt', $event)"
          (validityChange)="validity('prompt', $event)"
        />
      </div>
      <div data-field="context">
        <weave-ai-shared-context-picker
          [value]="step['context']"
          [references]="contextReferences"
          (valueChange)="edit('context', $event)"
        />
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
      <button type="button" (click)="profilesOpen = true">
        Configure workflow AI profiles
      </button>
      @if (profilesOpen) {
        <weave-modal
          heading="Workflow AI profiles"
          closeLabel="Close workflow AI profiles"
          [wide]="true"
          (dismiss)="closeProfile()"
        >
          <p class="hint">
            Profile changes stay in this draft until you review and apply them.
            All steps using the same named profile share its settings.
          </p>
          <weave-select
            label="Profile to edit"
            [options]="profileOptions"
            [value]="profileName"
            [disabled]="host.model.readonly || host.editingLocked"
            (choose)="chooseProfile($event)"
          />
          <button type="button" (click)="newProfile()">Add AI profile</button>
          @if (schema) {
            <weave-ai-setup-wizard
              [(step)]="wizardStep"
              [headings]="[
                'Configure the workflow model',
                'Choose a workflow connection slot',
                'Review workflow AI settings',
              ]"
              progressLabel="Workflow AI setup"
              finishLabel="Apply workflow AI settings"
              [canContinue]="canContinue"
              [navigationBlocked]="host.model.readonly || host.editingLocked"
              (finish)="saveProfile()"
            >
              <div
                ai-model
                (input)="profileTouched = true"
                (change)="profileTouched = true"
              >
                <label
                  >Profile name
                  <input
                    aria-label="AI profile name"
                    [value]="profileName"
                    [readOnly]="existingProfile"
                    (input)="profileName = value($event)"
                  />
                </label>
                @if (!nameValid) {
                  <p class="hint">
                    Choose a unique profile name using letters, numbers, dots,
                    underscores or hyphens.
                  </p>
                }
                <weave-ai-profile-editor
                  [schema]="schema"
                  [validateProfile]="validateProfile"
                  [initialData]="initialProfile"
                  (validityChange)="profileValid = $event"
                  (dataChange)="profile = $event"
                />
                <weave-schema-designer
                  [schema]="resultSchema"
                  heading="AI result fields"
                  [preview]="false"
                  (schemaChange)="resultSchema = $event; profileTouched = true"
                  (validityChange)="resultValid = $event"
                />
              </div>
              <div ai-connection>
                <div data-field="connection">
                  <weave-select
                    label="AI connection slot"
                    [options]="slotOptions"
                    [value]="draftSlot"
                    [disabled]="host.model.readonly || host.editingLocked"
                    (choose)="draftSlot = $event; profileTouched = true"
                  />
                </div>
                <p class="hint">
                  Activation binds this slot to an approved provider connection
                  in each environment. Provider credentials are never stored in
                  this workflow.
                </p>
                @if (!aiSlots.length && !newSlot) {
                  <p class="hint">Add an AI connection slot to continue.</p>
                }
                @if (newSlot) {
                  <p class="hint">
                    New slot {{ newSlot }} will be added when you apply these
                    settings.
                  </p>
                }
                <button type="button" (click)="addAiSlot()">
                  Add AI connection slot
                </button>
                @if (host.profile && host.can("connection.manage")) {
                  <button type="button" (click)="host.openAiConnectionDialog()">
                    New AI connection
                  </button>
                }
                <button type="button" (click)="host.openWorkflowSettings()">
                  Manage workflow connections
                </button>
              </div>
              <div ai-review>
                <dl>
                  <dt>Profile</dt>
                  <dd>{{ profileName }}</dd>
                  <dt>Provider</dt>
                  <dd>{{ profile["provider"] }}</dd>
                  <dt>Model or Azure deployment</dt>
                  <dd>{{ profile["model"] }}</dd>
                  <dt>Connection slot</dt>
                  <dd>{{ draftSlot }}</dd>
                </dl>
                <p class="hint">
                  Activation binds this slot to the environment connection; this
                  edit does not activate or run the workflow.
                </p>
                <p>These steps will use this profile:</p>
                <ul aria-label="Steps using this AI profile">
                  @for (id of affectedSteps; track id) {
                    <li>{{ id }}</li>
                  }
                </ul>
                @if (affectedSteps.length > 1) {
                  <p role="note">
                    Applying changes updates the shared model, reasoning,
                    generation limits and result fields for every listed step.
                  </p>
                }
              </div>
              <button ai-secondary type="button" (click)="cancelProfile()">
                Cancel profile changes
              </button>
            </weave-ai-setup-wizard>
          } @else if (loading) {
            <p role="status">Loading profile fields…</p>
          } @else {
            <button type="button" (click)="loadSchema()">
              Load profile fields
            </button>
          }
          @if (error) {
            <p class="field-error" role="alert">{{ error }}</p>
          }
        </weave-modal>
      }
      @if (applied) {
        <p role="status">Workflow AI settings applied.</p>
      }
      @if (error && !profilesOpen) {
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
      dd {
        margin: 0 0 12px;
        overflow-wrap: anywhere;
      }
      dt {
        font-weight: 600;
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
  profilesOpen = false;
  profileValid = false;
  resultValid = true;
  wizardStep = 0;
  draftSlot = "";
  newSlot = "";
  applied = false;
  readonly validateProfile = (profile: RecordValue) =>
    this.host.api.validateLlmProfile({ ...profile, outputSchema: {} });
  private loadedProfile = "";
  profileTouched = false;
  private profileBaseline = "";
  private readonly draftGuard = () =>
    this.actionLoading
      ? "Wait for the selected AI action to finish loading before continuing."
      : (this.profileTouched && (!this.profileValid || !this.resultValid)) ||
          this.profileSnapshot() !== this.profileBaseline
        ? "Apply or cancel the workflow AI profile changes before continuing. Your draft is still here."
        : "";
  private profileSnapshot() {
    return JSON.stringify([
      this.profileName,
      this.profile,
      this.resultSchema,
      this.draftSlot,
      this.newSlot,
    ]);
  }
  invalid = new Set<string>();
  actionLoading = false;
  async chooseAction(choice: ActionPickerChoice) {
    if (!choice.id || this.actionLoading) return;
    const scope = this.host.profile;
    this.actionLoading = true;
    this.error = "";
    try {
      const result = await this.host.api.request<RecordValue>(
        `${this.host.api.project}/actions/${encodeURIComponent(choice.id)}/export`,
      );
      if (!this.alive || scope !== this.host.profile) return;
      const document = object(result["document"] ?? result["definition"]);
      const implementation = object(object(document["spec"])["implementation"]);
      if (
        implementation["kind"] !== "worker" ||
        implementation["taskType"] !== "weave-agentic.generate" ||
        implementation["taskVersion"] !== "1.0.0"
      ) {
        this.error =
          "Choose an action implemented by the Agentic worker. The current action is unchanged.";
        return;
      }
      this.host.cacheContract(choice.uses, document);
      this.edit("uses", choice.uses);
      this.validity("uses", true);
    } catch (error) {
      if (this.alive) this.error = describeError(error).message;
    } finally {
      this.actionLoading = false;
      this.cdr.markForCheck();
    }
  }
  get step() {
    return JSON.parse(this.host.inspectorBuffer) as Step;
  }
  get profileOptions() {
    return this.profileNames.map((name) => ({ value: name, label: name }));
  }
  get slotOptions() {
    const slots = this.newSlot
      ? [...this.aiSlots, { name: this.newSlot, connector: aiConnector }]
      : this.aiSlots;
    return slots.map((slot) => ({
      value: slot.name,
      label: slot.name,
      description: slot.connector,
    }));
  }
  get aiSlots() {
    return compatibleSlots({ connector: aiConnector }, this.host.workflowSlots);
  }
  addAiSlot() {
    this.profileTouched = true;
    this.newSlot = slotName(
      "ai",
      this.host.workflowSlots.map((slot) => slot.name),
    );
    this.draftSlot = this.newSlot;
  }
  get nameValid() {
    return (
      /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(this.profileName) &&
      (!this.profiles[this.profileName] ||
        this.profileName === this.loadedProfile)
    );
  }
  get completeProfile() {
    return (
      this.nameValid &&
      this.profileValid &&
      this.resultValid &&
      !!this.schema &&
      missingData(this.schema, this.profile).length === 0
    );
  }
  get validSlot() {
    return (
      this.aiSlots.some((slot) => slot.name === this.draftSlot) ||
      (!!this.newSlot &&
        this.draftSlot === this.newSlot &&
        !this.host.workflowSlots.some((slot) => slot.name === this.newSlot))
    );
  }
  get canContinue() {
    return this.completeProfile && (this.wizardStep === 0 || this.validSlot);
  }
  get affectedSteps() {
    return this.host.model
      .nodes()
      .filter(
        ({ step }) =>
          step.kind === "llm" &&
          (step["profile"] === this.profileName || step.id === this.step.id),
      )
      .map(({ step }) => step.id);
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
    this.host.inspectorDraftGuard = this.draftGuard;
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
    if (this.host.inspectorDraftGuard === this.draftGuard)
      this.host.inspectorDraftGuard = null;
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
    this.host.touchInspector("step");
    const text = this.value(event);
    const [name, version, extra] = text.split("@");
    const valid =
      /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(name ?? "") &&
      validVersion(version ?? "") &&
      extra === undefined;
    this.validity(key, valid);
    if (valid) this.edit(key, text);
  }
  private keepProfileDraft() {
    const problem = this.draftGuard();
    if (problem) this.error = problem;
    return !!problem;
  }
  chooseProfile(name: string) {
    if (!this.keepProfileDraft()) this.loadProfile(name);
  }
  selectConnection(name: string) {
    if (!name || this.keepProfileDraft()) return;
    this.edit("connection", name);
    this.loadProfile(String(this.step["profile"] ?? ""));
  }
  selectProfile(name: string) {
    if (!name || this.keepProfileDraft()) return;
    this.edit("profile", name);
    this.loadProfile(name);
  }
  loadProfile(name: string) {
    this.profileName = name;
    this.loadedProfile = name;
    this.wizardStep = 0;
    this.draftSlot = String(this.step["connection"] ?? "");
    this.newSlot = "";
    this.applied = false;
    this.resultValid = true;
    this.existingProfile = !!this.profiles[name];
    const loaded = object(this.profiles[name]);
    this.resultSchema = object(loaded["outputSchema"]);
    if (!Object.keys(this.resultSchema).length)
      this.resultSchema = { type: "object", properties: {} };
    const { outputSchema: _, ...rest } = loaded;
    this.initialProfile = structuredClone(rest);
    this.profile = structuredClone(rest);
    // TaskForm clears field errors on reload without emitting initial validity.
    this.profileValid = true;
    this.profileTouched = false;
    this.profileBaseline = this.profileSnapshot();
  }
  newProfile() {
    if (this.keepProfileDraft()) return;
    let name = "default";
    let i = 1;
    while (this.profiles[name]) name = `profile-${i++}`;
    this.loadProfile(name);
    this.profileTouched = true;
  }
  closeProfile() {
    if (this.keepProfileDraft()) return;
    this.error = "";
    this.profilesOpen = false;
  }
  cancelProfile() {
    this.loadProfile(String(this.step["profile"] ?? ""));
    this.profilesOpen = false;
    this.error = "";
    this.host.error = "";
  }
  saveProfile() {
    if (
      !this.completeProfile ||
      !this.validSlot ||
      this.host.model.readonly ||
      this.host.editingLocked
    )
      return;
    this.host.flushInspector();
    const document = structuredClone(this.host.model.definition);
    document.spec["llmProfiles"] = {
      ...this.profiles,
      [this.profileName]: structuredClone({
        ...this.profile,
        outputSchema: this.resultSchema,
      }),
    };
    if (this.newSlot && this.draftSlot === this.newSlot) {
      document.spec["connections"] = {
        ...object(document.spec["connections"]),
        [this.newSlot]: { connector: aiConnector, required: true },
      };
    }
    this.host.model.batch(() => {
      this.host.workflowEdit({ value: document, path: "spec/llmProfiles" });
      this.host.flushInspector();
      this.host.stepEdit(
        { ...this.step, profile: this.profileName, connection: this.draftSlot },
        "profile",
      );
      this.host.flushInspector();
    });
    this.existingProfile = true;
    this.loadedProfile = this.profileName;
    this.newSlot = "";
    this.profilesOpen = false;
    this.applied = true;
    this.error = "";
    this.profileTouched = false;
    this.profileBaseline = this.profileSnapshot();
    this.host.error = "";
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
