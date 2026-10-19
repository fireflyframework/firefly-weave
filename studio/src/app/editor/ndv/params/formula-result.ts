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
// The Result panel under a focused Mapped field: "Result", the value's type,
// where its data comes from, and the value (200 characters, then Show all),
// or why it can't be shown, in words. It sits in the form's flow, so later
// fields move down and no drop target is covered. It isn't announced on
// every keystroke.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from "@angular/core";
import {
  SECRET,
  UNAVAILABLE,
  WAITING,
  previewText,
  valueTypeLabel,
} from "../../formula/preview";
import type { PreviewResult } from "../registry";

@Component({
  selector: "weave-formula-result",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  template: `<section
    class="formula-result"
    role="region"
    [attr.aria-label]="'Result of ' + label()"
  >
    <div class="formula-result-head">
      <span class="formula-result-title">Result</span>
      @if (type(); as type) {
        <span class="formula-result-type">{{ type }}</span>
      }
      @if (source()) {
        <span class="formula-result-source">{{ source() }}</span>
      }
    </div>
    @if (preview(); as result) {
      @if (result.ok) {
        <pre class="formula-result-value"
          >{{ shown().text }}{{ shown().truncated && !all() ? "…" : "" }}</pre
        >
        @if (shown().truncated || all()) {
          <button
            type="button"
            class="text-link formula-result-more"
            (click)="all.set(!all())"
          >
            {{ all() ? "Show less" : "Show all" }}
          </button>
        }
      } @else {
        <p class="formula-result-message" [attr.data-tone]="tone()">
          {{ result.error?.message }}
        </p>
      }
    }
    @if (tip()) {
      <p class="formula-result-tip">
        Tip: drag an input field from the left to use it here.
      </p>
    }
  </section>`,
})
export class FormulaResult {
  label = input.required<string>();
  preview = input<PreviewResult | null>(null);
  source = input("");
  tip = input(false);
  readonly all = signal(false);
  readonly shown = computed(() => {
    const result = this.preview();
    if (!result?.ok) return { text: "", truncated: false };
    const value = result.value ?? null;
    return this.all()
      ? { text: previewText(value, Infinity).text, truncated: true }
      : previewText(value);
  });
  readonly type = computed(() => {
    const result = this.preview();
    return result?.ok ? valueTypeLabel(result.value ?? null) : "";
  });
  readonly tone = computed(() => {
    const code = this.preview()?.error?.code;
    return code === WAITING || code === UNAVAILABLE || code === SECRET
      ? "neutral"
      : "danger";
  });
}
