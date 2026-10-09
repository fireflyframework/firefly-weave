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
// "Start a run": the run input is a form built from the activated
// version's input schema (with a JSON alternative), problems are explained in
// the dialog, and the last input per version is remembered in this browser.
import {
  ChangeDetectorRef,
  Component,
  OnChanges,
  SimpleChanges,
  inject,
  input,
  output,
} from "@angular/core";
import { Modal } from "../dialog";
import { describeError, PlainError } from "../errors";
import { StudioApi } from "../api";
import type { FileAccess } from "../forms/core/file-reference";
import { TaskForm } from "../task-form";
import { type Schema, groupedObject, missingRequired } from "../task-schema";
import { remember, remembered, rememberable } from "./run-memory";

export { rememberable } from "./run-memory";

/** The body of `POST …/runs`. */
export interface StartRunRequest {
  activation_id: string;
  input: Record<string, unknown>;
  business_key?: string;
  correlation_key?: string;
}
type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);
/**
 * Inputs: the API, the authorized activations, the activation to preselect,
 * whether a start is in flight, and the last failure (a new object each
 * time). Outputs: `start` with the request body, `cancel`. Retry run reuses
 * it with its own heading, wording, version labels and the run's input.
 */
@Component({
  selector: "weave-start-run-dialog",
  standalone: true,
  imports: [Modal, TaskForm],
  template: `<weave-modal
    [heading]="heading()"
    [closeLabel]="closeLabel()"
    describedBy="run-start-description"
    [wide]="hasForm && !asJson"
    (dismiss)="cancel.emit()"
  >
    <p id="run-start-description" class="dialog-message">
      @if (description()) {
        {{ description() }}
      } @else {
        Starts the active version in
        {{ workspace() || "this environment" }} with the input below.
      }
    </p>
    <!-- Problems are explained in words below, so the browser's bubbles are off. -->
    <form
      class="dialog-form start-run-form"
      novalidate
      (submit)="submit($event)"
    >
      <div class="field">
        <label for="run-version">Version to run</label>
        <select
          id="run-version"
          required
          [attr.aria-invalid]="versionProblem ? 'true' : null"
          [attr.aria-describedby]="versionProblem ? 'run-version-error' : null"
          (change)="choose(value($event))"
        >
          <option value="" [selected]="!activationId">
            Choose an active version
          </option>
          @for (a of activations(); track a["id"]) {
            <option [value]="a['id']" [selected]="a['id'] === activationId">
              {{ optionLabel(a) }}
            </option>
          }
        </select>
        @if (versionProblem) {
          <p class="field-error" id="run-version-error">
            {{ versionProblem }}
          </p>
        }
      </div>
      <details
        class="disclosure run-keys"
        [open]="!!businessKey || !!correlationKey"
      >
        <summary>Add a business key (optional)</summary>
        <div class="field">
          <label for="run-business-key">Business key</label>
          <input
            id="run-business-key"
            class="w-key"
            maxlength="200"
            aria-describedby="run-business-key-help"
            [value]="businessKey"
            (input)="businessKey = value($event)"
          />
          <p class="field-help" id="run-business-key-help">
            Your own reference, such as an order number. Use it to find this run
            later.
          </p>
        </div>
        <div class="field">
          <label for="run-correlation-key">Correlation key</label>
          <input
            id="run-correlation-key"
            class="w-key"
            maxlength="200"
            aria-describedby="run-correlation-key-help"
            [value]="correlationKey"
            (input)="correlationKey = value($event)"
          />
          <p class="field-help" id="run-correlation-key-help">
            Lets another system send signals to this run by key.
          </p>
        </div>
      </details>
      <fieldset class="run-input">
        <legend>Run input</legend>
        @if (loading) {
          <p class="hint" role="status">Loading the workflow's input form…</p>
        } @else if (note) {
          <p class="hint">{{ note }}</p>
        }
        @if (hasForm) {
          <label class="checkbox-field"
            ><input
              type="checkbox"
              [checked]="asJson"
              (change)="toggleJson()"
            />Edit as JSON</label
          >
        }
        @if (hasForm && !asJson) {
          @for (key of [formKey]; track key) {
            <!-- The seed only changes when the form is recreated: feeding
                 each change back would reset the form's own field errors. -->
            <weave-task-form
              [fileAccess]="fileAccess()"
              [schema]="schema"
              [initialData]="formSeed"
              (dataChange)="formData = $event; problem = ''"
              (validityChange)="formValid = $event"
            />
          }
        } @else {
          <label
            >Run input (JSON)<textarea
              class="monospace"
              spellcheck="false"
              [value]="jsonText"
              [attr.aria-invalid]="!!problem"
              [attr.aria-describedby]="problem ? 'run-input-problem' : null"
              (input)="jsonText = value($event); problem = ''"
            ></textarea>
          </label>
        }
        @if (problem) {
          <p id="run-input-problem" class="field-error" role="alert">
            {{ problem }}
          </p>
        }
      </fieldset>
      @if (failure(); as failed) {
        <div class="notice error-notice" role="alert">
          <p>The run didn't start. {{ failed.message }}</p>
          @if (failed.code) {
            <small class="support-code">Support code: {{ failed.code }}</small>
          }
        </div>
      }
      <div class="dialog-actions">
        <button type="button" (click)="cancel.emit()">Cancel</button>
        <button
          type="submit"
          class="primary"
          [attr.aria-disabled]="busy() ? 'true' : null"
        >
          {{ busy() ? busyLabel() : submitLabel() }}
        </button>
      </div>
    </form>
  </weave-modal>`,
  styles: [
    `
      .run-input {
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 10px 12px 12px;
        margin: 0;
        min-width: 0;
        display: grid;
        gap: 8px;
      }
      .run-input legend {
        font-weight: 600;
        padding: 0 4px;
      }
      .run-input textarea {
        min-height: 120px;
        width: 100%;
      }
      .start-run-form .field {
        margin-top: 12px;
      }
      .start-run-form .field label,
      .run-keys label {
        margin: 0;
      }
      .start-run-form select {
        width: 100%;
      }
      .run-keys {
        margin: 12px 0;
      }
      .run-keys .field {
        margin: 8px 0 12px;
      }
      .run-keys input {
        width: 100%;
      }
    `,
  ],
})
export class StartRunDialog implements OnChanges {
  fileAccess = input<FileAccess | null>(null);
  api = input.required<StudioApi>();
  activations = input<Record<string, unknown>[]>([]);
  initialActivationId = input("");
  busy = input(false);
  failure = input<PlainError | null>(null);
  /** "Payments / Production": where the run starts. */
  workspace = input("");
  /** Published versions by ID, for "todo-reader 1.0.0" options. */
  versions = input<ReadonlyMap<string, { name: string; version: string }>>(
    new Map(),
  );
  /** The version to choose first, for example the designer's workflow. */
  preselect = input<{ workflow: string; version: string } | null>(null);
  heading = input("Start a run");
  closeLabel = input("Close start a run");
  /** Replaces "Starts the active version in …" when set. */
  description = input("");
  submitLabel = input("Start run");
  busyLabel = input("Starting…");
  /** Option labels by activation ID, such as "Same version (1.0.0)". */
  labels = input<ReadonlyMap<string, string>>(new Map());
  /** The input to start from instead of the input remembered in this browser. */
  initialInput = input<Record<string, unknown> | null>(null);
  /** Business and correlation keys to start from. */
  initialKeys = input<{ business_key?: string; correlation_key?: string }>({});
  start = output<StartRunRequest>();
  cancel = output<void>();

  private cdr = inject(ChangeDetectorRef);
  activationId = "";
  businessKey = "";
  correlationKey = "";
  schema: Schema = {};
  hasForm = false;
  asJson = false;
  formData: Json = {};
  /** What a new form starts with: remembered input, or the JSON text. */
  formSeed: Json = {};
  formValid = true;
  /** Recreates the form when another version's schema is loaded. */
  formKey = 0;
  jsonText = "{}";
  loading = false;
  note = "";
  problem = "";
  /** "Choose a version to run.": shown under the version field. */
  versionProblem = "";
  /** The version whose input is remembered; the activation when unknown. */
  private memoryKey = "";
  /**
   * True once the version's input schema is loaded. Without it Studio can't
   * tell which values are secret, so the input is not remembered.
   */
  private schemaKnown = false;
  private generation = 0;
  /** True once the chosen activation was found in the list. */
  private resolved = false;

  ngOnChanges(changes: SimpleChanges) {
    if (changes["initialKeys"]?.firstChange) {
      this.businessKey = this.initialKeys().business_key ?? "";
      this.correlationKey = this.initialKeys().correlation_key ?? "";
    }
    if (
      changes["initialActivationId"]?.firstChange &&
      this.initialActivationId()
    )
      void this.choose(this.initialActivationId());
    // The list arrives after the dialog opens: resolve the preselected one.
    else if (changes["activations"] && this.activationId && !this.resolved)
      void this.choose(this.activationId);
    else if (changes["activations"] && !this.activationId && this.preselect()) {
      const wanted = this.preselect()!;
      const match = this.activations().find((a) => {
        const label = this.versionOf(a);
        return (
          label.name === wanted.workflow &&
          (!wanted.version || label.version === wanted.version)
        );
      });
      if (match) void this.choose(String(match["id"]));
    }
  }
  /** The workflow name and version an activation runs. */
  private versionOf(activation: Json) {
    const request = (activation["request"] ?? {}) as Json;
    const known = this.versions().get(String(request["version_id"] ?? ""));
    return {
      name: known?.name ?? String(activation["name"] ?? ""),
      version: known?.version ?? "",
    };
  }
  /** "todo-reader 1.0.0 · active since 2 Oct, 09:12". */
  optionLabel(activation: Json) {
    const label = this.labels().get(String(activation["id"] ?? ""));
    if (label) return label;
    const { name, version } = this.versionOf(activation);
    const since = this.since(
      activation["activated_at"] ?? activation["created_at"],
    );
    return [
      [name || "Unnamed workflow", version].filter(Boolean).join(" "),
      since
        ? `active since ${since}`
        : `revision ${activation["revision"] ?? 1}`,
    ].join(" · ");
  }
  private since(value: unknown) {
    if (typeof value !== "string" || !value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? ""
      : new Intl.DateTimeFormat("en-GB", {
          day: "numeric",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
          hourCycle: "h23",
        }).format(date);
  }
  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  /** Loads the chosen activation's input schema from its published version. */
  async choose(id: string) {
    const generation = ++this.generation;
    this.activationId = id;
    this.problem = "";
    this.versionProblem = "";
    this.hasForm = false;
    this.schema = {};
    this.note = "";
    this.loading = false;
    const activation = this.activations().find((a) => a["id"] === id);
    this.resolved = !!activation;
    const request = (activation?.["request"] ?? {}) as Json;
    const versionId =
      typeof request["version_id"] === "string" ? request["version_id"] : "";
    this.memoryKey = versionId || id;
    this.schemaKnown = false;
    const last = id
      ? (this.initialInput() ?? remembered(this.memoryKey))
      : null;
    this.jsonText = JSON.stringify(last ?? {}, null, 2);
    this.formData = last ?? {};
    this.formSeed = this.formData;
    this.formValid = true;
    if (!id) return;
    if (!versionId) {
      this.note = "Enter the run input as JSON.";
      return;
    }
    this.loading = true;
    this.cdr.markForCheck();
    try {
      const exported = await this.api().request<{ document?: Json }>(
        `${this.api().project}/workflows/${encodeURIComponent(versionId)}/export`,
      );
      if (generation !== this.generation) return;
      const spec = (exported.document?.["spec"] ?? {}) as Json;
      const schema = (spec["inputSchema"] ?? {}) as Schema;
      this.schema = schema;
      this.schemaKnown = true;
      // Input remembered before a field became secret is not shown again.
      if (last) {
        this.formData = rememberable(schema, last);
        this.jsonText = JSON.stringify(this.formData, null, 2);
      }
      this.formSeed = this.formData;
      this.formValid = true;
      this.hasForm = groupedObject(schema);
      this.formKey++;
      this.note = this.hasForm
        ? ""
        : "This workflow declares no input fields. Enter JSON if it accepts any.";
    } catch (e) {
      if (generation !== this.generation) return;
      this.note = `Studio couldn't load the workflow's input form, so enter the input as JSON. ${describeError(e).message}`;
    } finally {
      if (generation === this.generation) this.loading = false;
      this.cdr.markForCheck();
    }
  }
  toggleJson() {
    this.problem = "";
    if (this.asJson) {
      const parsed = this.parseJson();
      if (!parsed) {
        // Keep the JSON editor open until its text is a valid object.
        this.cdr.markForCheck();
        return;
      }
      this.formData = parsed;
      this.formSeed = parsed;
      this.formValid = true;
      this.formKey++;
    } else this.jsonText = JSON.stringify(this.formData, null, 2);
    this.asJson = !this.asJson;
    this.cdr.markForCheck();
  }
  private parseJson(): Json | null {
    let value: unknown;
    try {
      value = JSON.parse(this.jsonText || "{}");
    } catch {
      value = undefined;
    }
    if (isRecord(value)) return value;
    this.problem =
      'Enter the run input as a JSON object, for example {"customerId": "C-104"}.';
    return null;
  }
  submit(event: Event) {
    event.preventDefault();
    if (this.busy()) return;
    if (!this.activationId) {
      this.versionProblem = "Choose a version to run.";
      (event.target as HTMLFormElement)
        .querySelector<HTMLElement>("#run-version")
        ?.focus();
      this.cdr.markForCheck();
      return;
    }
    this.problem = "";
    let data: Json | null;
    if (this.hasForm && !this.asJson) {
      const missing = missingRequired(this.schema, this.formData);
      const form = event.target as HTMLFormElement;
      if (missing.length || !this.formValid) {
        this.problem = missing.length
          ? `Fill in ${missing.join(", ")}.`
          : "Correct the fields marked with an error.";
        form.querySelector<HTMLElement>(".run-input :invalid")?.focus();
        return;
      }
      data = this.formData;
    } else data = this.parseJson();
    if (!data) return;
    if (this.schemaKnown)
      remember(this.memoryKey, rememberable(this.schema, data));
    this.start.emit({
      activation_id: this.activationId,
      input: data,
      ...(this.businessKey ? { business_key: this.businessKey } : {}),
      ...(this.correlationKey ? { correlation_key: this.correlationKey } : {}),
    });
  }
}
