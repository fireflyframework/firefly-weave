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
// A signal's or a person's test answer, in the step's Output pane: what the
// signal carries (and after how long, or that it never comes), or what the
// person decides (or that the task expires). Executions use it instead of
// asking in the run log. Checked as all test data is: a value marked secret
// is refused and nothing is kept.
import {
  ChangeDetectionStrategy,
  Component,
  effect,
  input,
  signal,
  untracked,
} from "@angular/core";
import type { FormSession } from "../params/form-session";
import type { Json } from "../registry";

let sequence = 0;
const isObject = (value: unknown): value is Record<string, Json> =>
  !!value && typeof value === "object" && !Array.isArray(value);

@Component({
  selector: "weave-script-editor",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  template: `@let person = kind() === "humanTask";
    <section class="script-editor" [attr.aria-labelledby]="headingId">
      <h4 class="pane-caption" [id]="headingId">Test answer</h4>
      <p class="script-editor-note">
        {{
          person
            ? "Executing uses this decision instead of asking in the run log."
            : "Executing uses this signal instead of asking in the run log."
        }}
      </p>
      <label class="checkbox-field script-editor-switch">
        <input
          type="checkbox"
          [checked]="never()"
          (change)="never.set($any($event.target).checked)"
        />{{ person ? "Let it expire instead" : "Let it time out instead" }}
      </label>
      @if (!never()) {
        @if (person) {
          <fieldset class="script-editor-decisions">
            <legend>Decision</legend>
            @for (decision of decisions(); track decision) {
              <label class="script-editor-decision">
                <input
                  type="radio"
                  [name]="headingId"
                  [value]="decision"
                  [checked]="decision === decision$()"
                  (change)="decision$.set(decision)"
                />{{ decision }}
              </label>
            }
          </fieldset>
        } @else {
          <label class="script-editor-label" [for]="afterId"
            >After (seconds)</label
          >
          <input
            class="param-input script-editor-after"
            type="number"
            min="0"
            [id]="afterId"
            [value]="after()"
            (input)="after.set($any($event.target).value)"
          />
        }
        <label class="script-editor-label" [for]="jsonId">{{
          person ? "Form data (JSON)" : "Payload (JSON)"
        }}</label>
        <textarea
          class="param-input script-editor-json"
          rows="4"
          spellcheck="false"
          [id]="jsonId"
          [value]="json()"
          (input)="json.set($any($event.target).value)"
        ></textarea>
      }
      @if (problem()) {
        <p class="pane-problem" role="alert">{{ problem() }}</p>
      }
      <div class="pane-empty-actions script-editor-actions">
        <button type="button" class="sm primary" (click)="save()">
          Save answer
        </button>
        @if (saved()) {
          <button type="button" class="sm" (click)="remove()">
            Remove answer
          </button>
        }
      </div>
    </section>`,
})
export class ScriptEditor {
  readonly session = input.required<FormSession>();
  readonly headingId = `script-editor-${++sequence}`;
  readonly jsonId = `${this.headingId}-json`;
  readonly afterId = `${this.headingId}-after`;
  readonly json = signal("{}");
  readonly after = signal("");
  readonly never = signal(false);
  readonly decision$ = signal("");
  readonly problem = signal("");

  constructor() {
    // Another step, or the saved answer changed: the editor shows what is kept.
    effect(() => {
      const kept = this.saved();
      untracked(() => this.load(kept));
    });
  }
  kind(): string {
    const { controller, target } = this.session();
    return String(controller.step(target)?.kind ?? "");
  }
  decisions(): string[] {
    const { controller, target } = this.session();
    const step = controller.step(target);
    return Array.isArray(step?.["decisions"])
      ? (step["decisions"] as unknown[]).map(String)
      : [];
  }
  saved(): Json | null {
    const { controller, target } = this.session();
    return controller.scriptOf(target);
  }
  private load(kept: Json | null) {
    const entry = isObject(kept) ? kept : {};
    this.never.set(
      entry["outcome"] === "timeout" || entry["outcome"] === "expire",
    );
    const value =
      this.kind() === "humanTask" ? entry["data"] : entry["payload"];
    this.json.set(JSON.stringify(value ?? {}, null, 2));
    this.after.set(
      typeof entry["afterSeconds"] === "number"
        ? String(entry["afterSeconds"])
        : "",
    );
    this.decision$.set(
      typeof entry["decision"] === "string"
        ? entry["decision"]
        : (this.decisions()[0] ?? ""),
    );
    this.problem.set("");
  }
  async save() {
    const person = this.kind() === "humanTask";
    let entry: Record<string, Json>;
    if (this.never()) entry = { outcome: person ? "expire" : "timeout" };
    else {
      let value: Json;
      try {
        value = JSON.parse(this.json() || "{}") as Json;
      } catch {
        this.problem.set("This isn't JSON yet. Check the brackets and quotes.");
        return;
      }
      const after = Number(this.after());
      entry = person
        ? { decision: this.decision$(), data: value }
        : {
            payload: value,
            ...(this.after() && Number.isInteger(after) && after > 0
              ? { afterSeconds: after }
              : {}),
          };
    }
    const session = this.session();
    const outcome = await session.controller
      .ndvContext(session.target)
      .sample.setScript(entry);
    if (this.session() !== session) return;
    this.problem.set(outcome.ok ? "" : outcome.message);
    if (outcome.ok)
      session.announce(`Saved the test answer of ${session.target}.`);
  }
  async remove() {
    const session = this.session();
    await session.controller.ndvContext(session.target).sample.setScript(null);
  }
}
