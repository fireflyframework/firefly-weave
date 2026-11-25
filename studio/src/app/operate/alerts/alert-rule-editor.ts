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
// Adding or editing an alert rule: the signal and what it narrows to, the
// condition, how long it holds, the severity and the channels, with Preview
// beside the fields when the page holds both and between the fields and
// saving when it stacks. Preview reads the signal's current value for the
// draft without saving it. A percent is shown as 10 % and sent as 0.1.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  OnInit,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { ApiError } from "../../api";
import { describeError } from "../../errors";
import { Icon } from "../../icon";
import { alertSignals, type AlertChannel, type AlertRule } from "./alerts-api";
import {
  comparatorChoices,
  emptyRuleForm,
  forChoices,
  formFromRule,
  previewWords,
  renotifyChoices,
  ruleDraft,
  signalName,
  signalWords,
  windowChoices,
  type RuleForm,
  type RuleProblems,
} from "./alerts-model";
import type { RuleTarget } from "./alerts-page";
import type { AlertsStore } from "./alerts-store";

const selectorLabels = {
  workflow: ["Workflow", "Any workflow"],
  task_type: ["Task type", "Any task type"],
  target_id: ["Target", "Any target"],
} as const;

@Component({
  selector: "weave-alert-rule-editor",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  styleUrls: ["./alerts.css"],
  template: `<button
      type="button"
      class="tertiary back"
      (click)="done.emit(null)"
    >
      <weave-icon name="chevronLeft" [size]="16" />Back to rules
    </button>
    @let f = form();
    @let p = problems();
    <div class="editor">
      <section class="panel form ed-fields" aria-labelledby="rule-editor-title">
        <h2 id="rule-editor-title" tabindex="-1">{{ title() }}</h2>
        <div class="field">
          <label for="rule-name">Name</label>
          <input
            id="rule-name"
            maxlength="120"
            autocomplete="off"
            [value]="f.name"
            [attr.aria-invalid]="p.name ? 'true' : null"
            [attr.aria-describedby]="p.name ? 'rule-name-problem' : null"
            (input)="set('name', value($event))"
          />
          @if (p.name) {
            <p class="problem" id="rule-name-problem">
              <weave-icon name="failCircle" [size]="16" />{{ p.name }}
            </p>
          }
        </div>
        <div class="row2">
          <div class="field">
            <label for="rule-signal">Signal</label>
            <select
              id="rule-signal"
              [attr.aria-describedby]="
                (p.signal ? 'rule-signal-problem ' : '') +
                'rule-signal-hint' +
                (words()?.wholeEnvironment ? ' rule-whole' : '')
              "
              [attr.aria-invalid]="p.signal ? 'true' : null"
              (change)="setSignal(value($event))"
            >
              @if (!words()) {
                <option [value]="f.signal" selected>
                  {{ signalNameOf(f.signal) }}
                </option>
              }
              @for (signal of signals; track signal) {
                <option [value]="signal" [selected]="f.signal === signal">
                  {{ signalNameOf(signal) }}
                </option>
              }
            </select>
            <p class="hint" id="rule-signal-hint">
              {{
                words()
                  ? words()!.hint + " Test runs never count."
                  : "This server doesn't know this signal. Choose another to save the rule."
              }}
            </p>
            @if (p.signal) {
              <p class="problem" id="rule-signal-problem">
                <weave-icon name="failCircle" [size]="16" />{{ p.signal }}
              </p>
            }
          </div>
          @if (selector(); as key) {
            <div class="field">
              <label for="rule-selector"
                >{{ selectorWords()[0]
                }}<span class="opt">Optional</span></label
              >
              @if (key === "task_type") {
                <input
                  id="rule-selector"
                  autocomplete="off"
                  spellcheck="false"
                  [placeholder]="selectorWords()[1]"
                  [value]="f.selector"
                  [attr.aria-invalid]="p.selector ? 'true' : null"
                  [attr.aria-describedby]="
                    p.selector ? 'rule-selector-problem' : null
                  "
                  (input)="set('selector', value($event))"
                />
              } @else {
                <select
                  id="rule-selector"
                  [attr.aria-invalid]="p.selector ? 'true' : null"
                  [attr.aria-describedby]="
                    p.selector ? 'rule-selector-problem' : null
                  "
                  (change)="set('selector', value($event))"
                >
                  <option value="" [selected]="!f.selector">
                    {{ selectorWords()[1] }}
                  </option>
                  @for (choice of selectorChoices(); track choice.value) {
                    <option
                      [value]="choice.value"
                      [selected]="f.selector === choice.value"
                    >
                      {{ choice.label }}
                    </option>
                  }
                </select>
              }
              @if (p.selector) {
                <p class="problem" id="rule-selector-problem">
                  <weave-icon name="failCircle" [size]="16" />{{ p.selector }}
                </p>
              }
            </div>
          } @else if (words()?.wholeEnvironment) {
            <div class="field">
              <span class="field-label">Workflow</span>
              <p class="hint" id="rule-whole">
                Every workflow: this signal counts the whole environment.
              </p>
            </div>
          }
        </div>
        <div class="field" role="group" aria-labelledby="rule-condition-label">
          <span class="field-label" id="rule-condition-label"
            >Fires when the value is</span
          >
          <div class="condition">
            <select
              aria-label="Comparison"
              (change)="set('comparator', $any(value($event)))"
            >
              @for (choice of comparators; track choice.value) {
                <option
                  [value]="choice.value"
                  [selected]="f.comparator === choice.value"
                >
                  {{ choice.label }}
                </option>
              }
            </select>
            <span class="unit-input" [style.--unit-room]="unitRoom()">
              <input
                aria-label="Threshold"
                inputmode="decimal"
                autocomplete="off"
                [value]="f.threshold"
                [attr.aria-invalid]="p.threshold ? 'true' : null"
                [attr.aria-describedby]="
                  p.threshold ? 'rule-threshold-problem' : null
                "
                (input)="set('threshold', value($event))"
              />
              @if (unit()) {
                <span aria-hidden="true">{{ unit() }}</span>
              }
            </span>
            @if (words()?.window) {
              <select
                aria-label="Window"
                (change)="set('window', value($event))"
              >
                @for (choice of windows; track choice.value) {
                  <option
                    [value]="choice.value"
                    [selected]="f.window === choice.value"
                  >
                    {{ choice.label }}
                  </option>
                }
              </select>
            }
          </div>
          @if (p.threshold) {
            <p class="problem" id="rule-threshold-problem">
              <weave-icon name="failCircle" [size]="16" />{{ p.threshold }}
            </p>
          }
        </div>
        <div class="row2">
          <div class="field">
            <label for="rule-for">For</label>
            <select
              id="rule-for"
              aria-describedby="rule-for-hint"
              (change)="set('forSeconds', value($event))"
            >
              @for (choice of forOptions(); track choice.value) {
                <option
                  [value]="choice.value"
                  [selected]="f.forSeconds === choice.value"
                >
                  {{ choice.label }}
                </option>
              }
            </select>
            <p class="hint" id="rule-for-hint">
              How long the condition holds before the rule fires.
            </p>
          </div>
          @if (words()?.sampled) {
            <div class="field">
              <label for="rule-min">Only with at least</label>
              <span class="unit-input" style="--unit-room: 10ch">
                <input
                  id="rule-min"
                  inputmode="numeric"
                  autocomplete="off"
                  [value]="f.minSamples"
                  [attr.aria-invalid]="p.minSamples ? 'true' : null"
                  [attr.aria-describedby]="
                    p.minSamples ? 'rule-min-problem' : 'rule-min-unit'
                  "
                  (input)="set('minSamples', value($event))"
                />
                <span id="rule-min-unit">ended runs</span>
              </span>
              @if (p.minSamples) {
                <p class="problem" id="rule-min-problem">
                  <weave-icon name="failCircle" [size]="16" />{{ p.minSamples }}
                </p>
              }
            </div>
          }
        </div>
        <fieldset class="field severity">
          <legend class="field-label">Severity</legend>
          <div class="segments">
            @for (choice of severities; track choice.value) {
              <label
                ><input
                  type="radio"
                  name="rule-severity"
                  [value]="choice.value"
                  [checked]="f.severity === choice.value"
                  (change)="set('severity', choice.value)"
                /><weave-icon [name]="choice.icon" [size]="16" />{{
                  choice.label
                }}</label
              >
            }
          </div>
        </fieldset>
        <fieldset class="field">
          <legend class="field-label">
            Send to<span class="opt">Up to 5 email channels</span>
          </legend>
          @if (channels().length) {
            <div class="checks">
              @for (channel of channels(); track channel.id) {
                <label class="checkbox-field"
                  ><input
                    type="checkbox"
                    [checked]="f.channels.includes(channel.id)"
                    (change)="setChannel(channel.id, checked($event))"
                  />{{ channel.name
                  }}<span class="sub">{{
                    recipientCount(channel)
                  }}</span></label
                >
              }
            </div>
          } @else {
            <p class="hint">
              No email channels yet: add one on the Channels tab.
            </p>
          }
          @if (p.channels) {
            <p class="problem">
              <weave-icon name="failCircle" [size]="16" />{{ p.channels }}
            </p>
          }
          <p class="hint">
            Firing alerts always show on this page and in the navigation badge,
            with or without a channel.
          </p>
        </fieldset>
        <div class="field">
          <label for="rule-repeat">Repeat while firing</label>
          <select
            id="rule-repeat"
            aria-describedby="rule-repeat-hint"
            (change)="set('renotify', value($event))"
          >
            @for (choice of renotifyOptions(); track choice.value) {
              <option
                [value]="choice.value"
                [selected]="f.renotify === choice.value"
              >
                {{ choice.label }}
              </option>
            }
          </select>
          <p class="hint" id="rule-repeat-hint">
            Acknowledging an alert stops its repeats until it resolves.
          </p>
        </div>
      </section>
      <aside class="side" aria-labelledby="rule-preview-title">
        <h3 id="rule-preview-title">Preview</h3>
        @let s = store().preview();
        <button
          type="button"
          class="preview-button"
          [attr.aria-disabled]="
            !canPreview() || s.kind === 'loading' ? 'true' : null
          "
          [attr.aria-describedby]="canPreview() ? null : 'rule-preview-needs'"
          (click)="preview()"
        >
          Preview
        </button>
        @if (!canPreview()) {
          <p class="hint" id="rule-preview-needs">
            Preview also needs <code>status.read</code> in {{ environment() }}.
          </p>
        }
        <div
          class="preview-result"
          role="status"
          [attr.aria-busy]="s.kind === 'loading'"
        >
          @switch (s.kind) {
            @case ("loading") {
              <p class="hint">Reading the signal…</p>
            }
            @case ("ready") {
              @let w = previewText()!;
              <span class="cap muted">Evaluated now</span>
              <span class="value" [class.subtle]="s.preview.value === null">{{
                w.value
              }}</span>
              <p class="hint">{{ w.status }}</p>
              <p class="hint">{{ w.detail }}</p>
              <dl>
                <dt>Signal</dt>
                <dd>
                  {{
                    previewSignal().name
                      ? previewSignal().signal + ","
                      : previewSignal().signal
                  }}
                  @if (previewSignal().name) {
                    <span class="nb">{{ previewSignal().name }}</span>
                  }
                </dd>
                @if (s.draft.window_seconds) {
                  <dt>Window</dt>
                  <dd>{{ windowName(s.draft.window_seconds) }}</dd>
                }
                @if (s.preview.samples !== null) {
                  <dt>Ended runs</dt>
                  <dd>{{ s.preview.samples }} (test runs don't count)</dd>
                }
              </dl>
            }
            @case ("error") {
              <p class="problem">
                <weave-icon name="failCircle" [size]="16" />{{
                  s.error.message
                }}
              </p>
            }
            @default {
              <p class="hint">
                Preview reads the signal's value now for this rule, without
                saving it.
              </p>
            }
          }
        </div>
      </aside>
      <div class="ed-save">
        <p class="says">{{ saying() }}</p>
        @if (saveError()) {
          <p class="problem" role="alert">
            <weave-icon name="failCircle" [size]="16" />{{ saveError() }}
          </p>
        }
        <div class="formacts">
          <button
            type="button"
            class="primary"
            [attr.aria-disabled]="saving() ? 'true' : null"
            (click)="save()"
          >
            Save rule
          </button>
          <button type="button" class="tertiary" (click)="done.emit(null)">
            Cancel
          </button>
        </div>
      </div>
    </div>`,
  styles: `
    .back {
      margin: 0 0 var(--space-4) calc(-1 * var(--space-2));
    }
    .editor {
      display: grid;
      grid-template-columns: minmax(0, 720px);
      grid-template-areas: "fields" "side" "save";
      gap: var(--space-4) var(--space-6);
      align-items: start;
    }
    .ed-fields {
      grid-area: fields;
    }
    .side {
      grid-area: side;
    }
    .ed-save {
      grid-area: save;
      display: flex;
      flex-direction: column;
      gap: var(--space-3);
    }
    /* The form beside Preview once the page holds both (1336 px of window
       with the full navigation). */
    @media (min-width: 1336px) {
      .editor {
        grid-template-columns: minmax(0, 720px) minmax(320px, 1fr);
        grid-template-areas: "fields side" "save side";
      }
      /* Beside the fields, Preview stays in view while they scroll; stacked,
         it would cover Save. */
      .side {
        position: sticky;
        top: 0;
      }
    }
    .panel {
      padding: var(--space-6);
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--surface);
      min-width: 0;
    }
    .panel h2:focus {
      outline: none;
    }
    .side {
      display: flex;
      flex-direction: column;
      gap: var(--space-3);
      padding: var(--space-5);
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--surface);
      min-width: 0;
    }
    .side h3 {
      margin: 0;
      font: var(--type-title-sm);
    }
    .preview-button {
      align-self: flex-start;
    }
    .preview-result {
      display: flex;
      flex-direction: column;
      gap: var(--space-3);
    }
    .cap {
      font: var(--type-caption);
    }
    .value {
      font: 600 28px/36px var(--font-sans);
      font-variant-numeric: tabular-nums;
    }
    dl {
      display: grid;
      grid-template-columns: max-content minmax(0, 1fr);
      gap: 6px var(--space-4);
      margin: 0;
      font: var(--type-small);
    }
    dt {
      color: var(--muted);
    }
    dd {
      margin: 0;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    fieldset {
      margin: 0;
      padding: 0;
      border: 0;
      min-width: 0;
    }
    legend {
      padding: 0;
      margin-bottom: 6px;
    }
    @media (max-width: 767px) {
      .panel {
        padding: var(--space-4);
      }
      dl {
        grid-template-columns: minmax(0, 1fr);
        gap: 2px;
      }
      dd {
        margin-bottom: var(--space-2);
      }
    }
  `,
})
export class AlertRuleEditor implements OnInit {
  target = input.required<RuleTarget>();
  store = input.required<AlertsStore>();
  channels = input<AlertChannel[]>([]);
  workflowNames = input<string[]>([]);
  targets = input<Map<string, string>>(new Map());
  environment = input("this environment");
  canPreview = input(true);
  /** The saved rule, or null when the person went back without saving. */
  done = output<AlertRule | null>();
  private element = inject(ElementRef<HTMLElement>);
  private injector = inject(Injector);
  readonly signals = alertSignals;
  readonly comparators = comparatorChoices;
  readonly windows = windowChoices;
  readonly severities = [
    { value: "info", label: "Info", icon: "info" },
    { value: "warning", label: "Warning", icon: "warning" },
    { value: "critical", label: "Critical", icon: "critical" },
  ] as const;
  readonly form = signal<RuleForm>(emptyRuleForm());
  readonly problems = signal<RuleProblems>({});
  readonly saving = signal(false);
  readonly saveError = signal("");
  /** One key per rule this editor adds, so a retried save never adds two. */
  private readonly key = crypto.randomUUID();

  readonly words = computed(() => signalWords(this.form().signal));
  readonly selector = computed(() => this.words()?.selector ?? null);
  readonly selectorWords = computed(
    () => selectorLabels[this.selector() ?? "workflow"],
  );
  readonly unit = computed(() => {
    const unit = this.words()?.unit;
    return unit === "ratio" ? "%" : unit === "seconds" ? "s" : "";
  });
  readonly unitRoom = computed(() => (this.unit() ? "2ch" : "0ch"));
  readonly forOptions = computed(() => forChoices(this.form().forSeconds));
  readonly renotifyOptions = computed(() =>
    renotifyChoices(this.form().renotify),
  );
  readonly title = computed(() => {
    const target = this.target();
    return target.kind === "rule" ? `Edit ${target.rule.name}` : "Add a rule";
  });
  /** The choices of the Workflow or Target menu, with the saved one kept. */
  readonly selectorChoices = computed(() => {
    const key = this.selector();
    const current = this.form().selector;
    if (key === "target_id") {
      const entries = [...this.targets()].map(([value, label]) => ({
        value,
        label,
      }));
      if (current && !this.targets().has(current))
        entries.push({
          value: current,
          label: `Target ${current.slice(0, 8)}`,
        });
      return entries;
    }
    const names = new Set(this.workflowNames());
    if (current) names.add(current);
    return [...names].sort().map((name) => ({ value: name, label: name }));
  });
  readonly previewText = computed(() => {
    const state = this.store().preview();
    return state.kind === "ready"
      ? previewWords(state.preview, state.draft)
      : null;
  });
  readonly previewSignal = computed(() => {
    const state = this.store().preview();
    if (state.kind !== "ready") return { signal: "", name: "" };
    const key = signalWords(state.draft.signal)?.selector;
    const narrowed = key ? state.draft.selector[key] : undefined;
    const name =
      key === "target_id" && narrowed
        ? (this.targets().get(narrowed) ?? narrowed)
        : narrowed;
    return { signal: signalName(state.draft.signal), name: name ?? "" };
  });
  readonly saying = computed(() => {
    const mails = this.form().channels.length > 0;
    const environment = this.environment();
    return mails
      ? `Saving checks this rule every 30 seconds in ${environment}. When it fires, it shows here, rings the bell in the navigation if it's critical, and mails the channels you chose; when it clears for a minute, it resolves and mails once more.`
      : `Saving checks this rule every 30 seconds in ${environment}. When it fires, it shows here and rings the bell in the navigation if it's critical; when it clears for a minute, it resolves.`;
  });

  ngOnInit() {
    const target = this.target();
    this.form.set(
      target.kind === "rule"
        ? formFromRule(target.rule)
        : target.kind === "preset"
          ? formFromRule(target.preset.rule)
          : emptyRuleForm(),
    );
    this.store().clearPreview();
    // Opening the editor moves focus to its heading.
    afterNextRender(
      () =>
        (
          this.element.nativeElement.querySelector(
            "#rule-editor-title",
          ) as HTMLElement | null
        )?.focus(),
      { injector: this.injector },
    );
  }

  signalNameOf(signal: string) {
    return signalName(signal);
  }
  windowName(seconds: number) {
    return (
      windowChoices
        .find((choice) => choice.value === String(seconds))
        ?.label.replace("over the last ", "")
        .replace(/^minute$/, "1 minute")
        .replace(/^hour$/, "1 hour") ?? `${seconds} s`
    );
  }
  recipientCount(channel: AlertChannel) {
    const count = channel.recipients.length;
    return `${count} ${count === 1 ? "recipient" : "recipients"}`;
  }
  value(event: Event) {
    return (event.target as HTMLInputElement | HTMLSelectElement).value;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }

  /** A changed field: its problem goes, and an older preview no longer applies. */
  set<K extends keyof RuleForm>(field: K, value: RuleForm[K]) {
    this.form.update((form) => ({ ...form, [field]: value }));
    if (this.problems()[field])
      this.problems.update((problems) => ({ ...problems, [field]: undefined }));
    this.store().clearPreview();
  }
  setSignal(signal: string) {
    const before = this.words();
    const after = signalWords(signal);
    this.form.update((form) => ({
      ...form,
      signal,
      selector: before?.selector === after?.selector ? form.selector : "",
      // A percent means nothing for a count, and the other way around.
      threshold: before?.unit === after?.unit ? form.threshold : "",
    }));
    this.problems.set({});
    this.store().clearPreview();
  }
  setChannel(id: string, on: boolean) {
    const channels = this.form().channels.filter((item) => item !== id);
    this.set("channels", on ? [...channels, id] : channels);
  }

  private draft() {
    const result = ruleDraft(this.form());
    if ("problems" in result) {
      this.problems.set(result.problems);
      afterNextRender(
        () =>
          (
            this.element.nativeElement.querySelector(
              '[aria-invalid="true"]',
            ) as HTMLElement | null
          )?.focus(),
        { injector: this.injector },
      );
      return null;
    }
    return result.draft;
  }
  async preview() {
    if (!this.canPreview() || this.store().preview().kind === "loading") return;
    const draft = this.draft();
    if (draft) await this.store().runPreview(draft);
  }
  async save() {
    if (this.saving()) return;
    const draft = this.draft();
    if (!draft) return;
    this.saving.set(true);
    this.saveError.set("");
    const target = this.target();
    try {
      const saved = await this.store().saveRule(
        draft,
        target.kind === "rule"
          ? { id: target.rule.id, revision: target.rule.revision }
          : null,
        this.key,
      );
      if (saved) this.done.emit(saved);
    } catch (error) {
      this.refused(error);
    } finally {
      this.saving.set(false);
    }
  }
  /** A refusal names its field when the platform says which one. */
  private refused(error: unknown) {
    const plain = describeError(error);
    const detail =
      error instanceof ApiError &&
      error.detail &&
      typeof error.detail === "object"
        ? (error.detail as { result?: Record<string, unknown> }).result
        : undefined;
    const field = typeof detail?.["field"] === "string" ? detail["field"] : "";
    const map: Record<string, keyof RuleForm> = {
      name: "name",
      selector: "selector",
      threshold: "threshold",
      window_seconds: "window",
      for_seconds: "forSeconds",
      min_samples: "minSamples",
      channel_ids: "channels",
    };
    const shown = map[field.split(".")[0]];
    if (
      plain.code === "WV-ALERT-SIGNAL" &&
      shown &&
      shown !== "window" &&
      shown !== "forSeconds"
    ) {
      this.problems.update((problems) => ({
        ...problems,
        [shown]: plain.message,
      }));
      return;
    }
    this.saveError.set(
      plain.code ? `${plain.code}: ${plain.message}` : plain.message,
    );
  }
}
