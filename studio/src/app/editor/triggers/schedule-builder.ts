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
// When a schedule runs: a preset (every N minutes, daily, weekly, monthly)
// or a custom UTC calendar, checked by the platform's rules, with the next
// five times it runs. Each change is written to the schedule's `cron` as an
// undo step, like any field of step details.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from "@angular/core";
import type { FormSession } from "../ndv/params/form-session";
import type { TriggerIntent } from "../state/canvas-sidecar";
import {
  EVERY_MINUTES,
  WEEKDAYS,
  presetCron,
  readPreset,
  type SchedulePreset,
} from "./schedule";
import { schedulePreview } from "./trigger-forms";

type Repeat = SchedulePreset["kind"];
const REPEATS: readonly { value: Repeat; label: string }[] = [
  { value: "minutes", label: "Every N minutes" },
  { value: "daily", label: "Daily at" },
  { value: "weekly", label: "Weekly on" },
  { value: "monthly", label: "Monthly on day" },
  { value: "custom", label: "Custom UTC cron" },
];

const clock = (hour: number, minute: number) =>
  `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;

@Component({
  selector: "weave-schedule-builder",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  template: `<div class="param schedule-builder" data-param="cron">
    <div class="param-label-row">
      <span class="param-label" [id]="ids.label">When it runs</span>
      <span class="param-required" aria-hidden="true">*</span>
    </div>
    <div class="schedule-row">
      <div class="schedule-field">
        <label class="schedule-caption" [for]="ids.repeats">Repeats</label>
        <select
          [id]="ids.repeats"
          class="param-input"
          [value]="preset().kind"
          (change)="repeat($any($event.target).value)"
        >
          @for (choice of repeats; track choice.value) {
            <option [value]="choice.value">{{ choice.label }}</option>
          }
        </select>
      </div>
      @switch (preset().kind) {
        @case ("minutes") {
          <div class="schedule-field">
            <label class="schedule-caption" [for]="ids.every">Every</label>
            <select
              [id]="ids.every"
              class="param-input"
              [value]="minutes()"
              (change)="every(+$any($event.target).value)"
            >
              @for (n of everyMinutes; track n) {
                <option [value]="n">
                  {{ n === 1 ? "1 minute" : n + " minutes" }}
                </option>
              }
            </select>
          </div>
        }
        @case ("weekly") {
          <div class="schedule-field">
            <label class="schedule-caption" [for]="ids.day">Day</label>
            <select
              [id]="ids.day"
              class="param-input"
              [value]="weekday()"
              (change)="day(+$any($event.target).value)"
            >
              @for (name of weekdays; track $index) {
                <option [value]="$index">{{ name }}</option>
              }
            </select>
          </div>
        }
        @case ("monthly") {
          <div class="schedule-field">
            <label class="schedule-caption" [for]="ids.monthday"
              >Day of month</label
            >
            <input
              [id]="ids.monthday"
              class="param-input"
              type="number"
              min="1"
              max="31"
              [value]="monthDay()"
              (change)="day(+$any($event.target).value)"
            />
          </div>
        }
        @case ("custom") {
          <div class="schedule-field schedule-cron">
            <label class="schedule-caption" [for]="ids.cron">Cron</label>
            <input
              [id]="ids.cron"
              class="param-input"
              autocomplete="off"
              spellcheck="false"
              placeholder="0 9 * * 1"
              [value]="cron()"
              (change)="custom($any($event.target).value)"
            />
          </div>
        }
      }
      @if (timed()) {
        <div class="schedule-field">
          <label class="schedule-caption" [for]="ids.at">At (UTC)</label>
          <input
            [id]="ids.at"
            class="param-input"
            type="time"
            [value]="time()"
            (change)="at($any($event.target).value)"
          />
        </div>
      }
    </div>
    @if (preview(); as shown) {
      @if ("problem" in shown) {
        <p class="param-line" data-kind="error" role="alert">
          {{ shown.problem }}
        </p>
      } @else {
        <p class="schedule-name">{{ shown.name }}</p>
        <ul class="schedule-next" aria-label="Next five times">
          @for (time of shown.next; track $index) {
            <li>{{ time }}</li>
          }
        </ul>
        @if (shown.note) {
          <p class="param-line" data-kind="hint">{{ shown.note }}</p>
        }
      }
    } @else {
      <p class="param-line" data-kind="hint">Choose when the schedule runs.</p>
    }
  </div>`,
  styles: `
    .schedule-row {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-3);
    }
    .schedule-field {
      display: flex;
      flex-direction: column;
      gap: var(--space-1);
      min-width: 9rem;
      flex: 1 1 9rem;
    }
    .schedule-cron {
      flex-basis: 14rem;
    }
    .schedule-caption {
      font: var(--type-small);
      color: var(--muted);
    }
    .schedule-name {
      margin: 0;
      font: var(--type-label);
    }
    .schedule-next {
      margin: 0;
      padding-left: var(--space-4);
      font: var(--type-small);
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }
  `,
})
export class ScheduleBuilder {
  readonly session = input.required<FormSession>();
  readonly trigger = input.required<TriggerIntent>();
  readonly repeats = REPEATS;
  readonly everyMinutes = EVERY_MINUTES;
  readonly weekdays = WEEKDAYS;
  private readonly prefix = `schedule-${Math.random().toString(36).slice(2)}`;
  readonly ids = {
    label: `${this.prefix}-when`,
    repeats: `${this.prefix}-repeats`,
    every: `${this.prefix}-every`,
    day: `${this.prefix}-day`,
    monthday: `${this.prefix}-monthday`,
    cron: `${this.prefix}-cron`,
    at: `${this.prefix}-at`,
  };
  /** The repeat chosen before a calendar is written (custom with no text yet). */
  private readonly chosen = signal<Repeat | null>(null);

  cron(): string {
    const value = this.trigger()["cron"];
    return typeof value === "string" ? value : "";
  }
  readonly preset = computed((): SchedulePreset => {
    const cron = this.cron();
    const read = cron ? readPreset(cron) : null;
    const chosen = this.chosen();
    if (chosen === "custom" || (!read && chosen === null))
      return { kind: "custom", cron };
    return read ?? { kind: "custom", cron };
  });
  minutes(): number {
    const preset = this.preset();
    return preset.kind === "minutes" ? preset.every : 15;
  }
  weekday(): number {
    const preset = this.preset();
    return preset.kind === "weekly" ? preset.day : 1;
  }
  monthDay(): number {
    const preset = this.preset();
    return preset.kind === "monthly" ? preset.day : 1;
  }
  timed(): boolean {
    const kind = this.preset().kind;
    return kind === "daily" || kind === "weekly" || kind === "monthly";
  }
  time(): string {
    const preset = this.preset();
    return "hour" in preset ? clock(preset.hour, preset.minute) : "09:00";
  }
  preview() {
    const cron = this.cron();
    return cron ? schedulePreview(cron, new Date()) : null;
  }

  private write(preset: SchedulePreset) {
    const cron = presetCron(preset);
    this.session().commit(
      [{ scope: "step", path: ["cron"], value: cron || undefined }],
      "cron",
    );
  }
  repeat(kind: Repeat) {
    this.chosen.set(kind);
    const [hour, minute] = this.time().split(":").map(Number);
    if (kind === "custom") return;
    if (kind === "minutes") this.write({ kind, every: this.minutes() });
    else if (kind === "daily") this.write({ kind, hour, minute });
    else if (kind === "weekly")
      this.write({ kind, day: this.weekday(), hour, minute });
    else this.write({ kind, day: this.monthDay(), hour, minute });
  }
  every(every: number) {
    this.write({ kind: "minutes", every });
  }
  day(day: number) {
    const preset = this.preset();
    if (preset.kind === "weekly" || preset.kind === "monthly")
      this.write({ ...preset, day });
  }
  at(value: string) {
    const [hour, minute] = value.split(":").map(Number);
    const preset = this.preset();
    if (Number.isInteger(hour) && Number.isInteger(minute) && "hour" in preset)
      this.write({ ...preset, hour, minute });
  }
  custom(value: string) {
    this.chosen.set("custom");
    this.write({ kind: "custom", cron: value });
  }
}
