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
  ChangeDetectorRef,
  Component,
  ElementRef,
  Input,
  inject,
  output,
} from "@angular/core";

/** Shared navigation only; each caller retains its draft and save authority. */
@Component({
  selector: "weave-ai-setup-wizard",
  standalone: true,
  template: `
    <ol class="settings-progress" [attr.aria-label]="progressLabel">
      @for (label of labels; track $index) {
        <li
          [attr.aria-current]="step === $index ? 'step' : null"
          [class.complete]="step > $index"
        >
          <span class="step-number" aria-hidden="true">{{ $index + 1 }}</span
          >{{ label }}
        </li>
      }
    </ol>
    <h3 class="settings-heading" tabindex="-1">{{ headings[step] }}</h3>
    <!-- Keep projected forms mounted so Back preserves their draft and validation. -->
    <div [hidden]="step !== 0"><ng-content select="[ai-model]" /></div>
    <div [hidden]="step !== 1"><ng-content select="[ai-connection]" /></div>
    <div [hidden]="step !== 2"><ng-content select="[ai-review]" /></div>
    <ng-content select="[ai-error]" />
    <div class="wizard-actions">
      @if (step > 0) {
        <button
          type="button"
          [disabled]="busy || navigationBlocked"
          (click)="move(-1)"
        >
          Back
        </button>
      }
      @if (step < 2) {
        <button
          type="button"
          class="primary"
          [disabled]="busy || navigationBlocked || !canContinue"
          (click)="move(1)"
        >
          {{ nextLabels[step] }}
        </button>
      } @else {
        <button
          type="button"
          class="primary"
          [disabled]="busy || navigationBlocked || !canContinue"
          (click)="complete()"
        >
          {{ busy ? "Saving…" : finishLabel }}
        </button>
      }
    </div>
    <ng-content select="[ai-secondary]" />
  `,
  styles: `
    :host {
      display: grid;
      gap: 16px;
      min-width: 0;
    }
    [hidden] {
      display: none !important;
    }
    .wizard-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding-top: 16px;
      border-top: 1px solid var(--line);
    }
    .settings-progress {
      display: flex;
      gap: 16px;
      padding: 0 0 16px;
      margin: 0;
      list-style: none;
      border-bottom: 1px solid var(--line);
      flex-wrap: wrap;
    }
    .settings-progress li {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
    }
    .settings-progress [aria-current] {
      color: var(--text);
      font-weight: 600;
    }
    .step-number {
      display: grid;
      place-items: center;
      width: 28px;
      height: 28px;
      border-radius: 50%;
      border: 1px solid var(--field-border);
    }
    [aria-current] .step-number {
      color: var(--on-accent);
      background: var(--accent);
      border-color: var(--accent);
    }
    .complete .step-number {
      background: var(--selected);
    }
    .settings-heading {
      margin: 0;
    }

    @media (max-width: 480px) {
      .settings-progress {
        gap: 12px;
        font-size: 12px;
      }
      .settings-progress li {
        gap: 4px;
      }
      .step-number {
        width: 22px;
        height: 22px;
      }
    }
  `,
})
export class AiSetupWizard {
  @Input() step = 0;
  @Input() labels = ["Model", "Connection", "Review"];
  @Input() headings = [
    "Choose a model",
    "Choose a connection",
    "Review AI settings",
  ];
  @Input() nextLabels = ["Continue to connection", "Review settings"];
  @Input() finishLabel = "Apply settings";
  @Input() progressLabel = "AI setup progress";
  @Input() canContinue = false;
  @Input() busy = false;
  @Input() navigationBlocked = false;
  stepChange = output<number>();
  finish = output<void>();
  private cdr = inject(ChangeDetectorRef);
  private element = inject<ElementRef<HTMLElement>>(ElementRef);
  move(direction: -1 | 1) {
    if (
      this.busy ||
      this.navigationBlocked ||
      (direction === 1 && !this.canContinue)
    )
      return;
    this.step = Math.max(0, Math.min(2, this.step + direction));
    this.stepChange.emit(this.step);
    this.cdr.detectChanges();
    this.element.nativeElement
      .querySelector<HTMLElement>(".settings-heading")
      ?.focus();
  }
  complete() {
    if (
      this.step === 2 &&
      this.canContinue &&
      !this.busy &&
      !this.navigationBlocked
    )
      this.finish.emit();
  }
}
