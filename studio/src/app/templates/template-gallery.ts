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
// "Start from a template" rows for Home and the New menu. The
// documents load from a separate chunk on first display; choosing one only
// emits it, and the host opens it as a new, unsaved draft.
import {
  ChangeDetectorRef,
  Component,
  OnInit,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { Icon } from "../icon";
import type { Kind } from "../model";
import { stepKindLabels } from "../designer/step-kinds";
import type { WorkflowTemplate } from "./catalog";

/** What the host receives: the template identity and its YAML source. */
export interface TemplateChoice {
  id: string;
  title: string;
  yaml: string;
}

/** Loads the template documents (a lazily split chunk). */
export async function loadWorkflowTemplates(): Promise<
  readonly WorkflowTemplate[]
> {
  return (await import("./catalog")).workflowTemplates;
}

let sequence = 0;

@Component({
  selector: "weave-template-gallery",
  standalone: true,
  imports: [Icon],
  template: `<section
    class="template-gallery"
    [attr.aria-labelledby]="prefix + '-title'"
  >
    <div class="template-heading">
      <h2 [id]="prefix + '-title'">{{ heading() }}</h2>
      <p class="hint">
        {{
          local()
            ? "Opens as a new draft on this computer."
            : "Each template opens as a new draft."
        }}
      </p>
    </div>
    @switch (state()) {
      @case ("loading") {
        <p class="hint" role="status">Loading templates…</p>
      }
      @case ("error") {
        <div class="notice error-notice" role="alert">
          <p>Studio couldn't load the templates.</p>
          <button type="button" (click)="load()">Try again</button>
        </div>
      }
      @default {
        <ul class="template-rows">
          @if (showBlank()) {
            <li class="template-row">
              <span class="glyph blank" aria-hidden="true"
                ><span class="glyph-box"></span
              ></span>
              <span class="template-text">
                <h3 [id]="prefix + '-blank'">Blank workflow</h3>
                <p [id]="prefix + '-blank-description'">
                  Start with an empty canvas and add steps yourself.
                </p>
              </span>
              <span class="trailing">
                <button
                  type="button"
                  class="tertiary"
                  [disabled]="disabled()"
                  [attr.aria-describedby]="prefix + '-blank-description'"
                  (click)="blank.emit()"
                >
                  Start blank
                </button>
              </span>
            </li>
          }
          @for (template of templates(); track template.id) {
            <li class="template-row" [attr.data-template]="template.id">
              <span
                class="glyph"
                [attr.title]="steps(template.kinds)"
                aria-hidden="true"
              >
                @for (
                  kind of glyph(template.kinds);
                  track kind;
                  let last = $last
                ) {
                  <weave-icon [name]="kind" [size]="16" />
                  @if (!last) {
                    <span class="glyph-line"></span>
                  }
                }
              </span>
              <span class="template-text">
                <h3>{{ template.title }}</h3>
                <p [id]="prefix + '-' + template.id">
                  {{ template.description }}
                  <span class="sr-only"
                    >Steps: {{ steps(template.kinds) }}.</span
                  >
                </p>
                @if (template.note) {
                  <p class="template-note">{{ template.note }}</p>
                }
              </span>
              <span class="trailing">
                @if (template.placeholder) {
                  <span class="status-pill" data-tone="warning"
                    >You choose the action</span
                  >
                }
                <button
                  type="button"
                  class="tertiary use-template"
                  [disabled]="disabled()"
                  [attr.aria-label]="'Use template: ' + template.title"
                  [attr.aria-describedby]="prefix + '-' + template.id"
                  (click)="pick(template)"
                >
                  Use template
                </button>
              </span>
            </li>
          }
        </ul>
      }
    }
  </section>`,
  styles: [
    `
      :host {
        display: block;
      }
      .template-heading h2 {
        margin: 0 0 4px;
        font: var(--type-title-sm);
      }
      .template-heading .hint {
        margin: 0 0 12px;
      }
      .template-rows {
        list-style: none;
        margin: 0;
        padding: 0;
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        background: var(--surface);
        overflow: hidden;
      }
      /* The whole row starts the template: "Use template" covers it. */
      .template-row {
        position: relative;
        display: grid;
        grid-template-columns: 120px minmax(0, 1fr) auto;
        align-items: center;
        gap: var(--space-4);
        min-height: 64px;
        padding: var(--space-3) var(--space-4);
        border-bottom: 1px solid var(--line);
      }
      .template-row:last-child {
        border-bottom: 0;
      }
      .template-row:hover {
        background: var(--hover);
      }
      .template-row:has(button:focus-visible) {
        outline: 2px solid var(--focus);
        outline-offset: -2px;
      }
      .template-row button::after {
        content: "";
        position: absolute;
        inset: 0;
      }
      .template-row button:focus-visible {
        outline: none;
      }
      .glyph {
        display: flex;
        align-items: center;
        width: 120px;
        height: 32px;
        color: var(--muted);
      }
      .glyph-line {
        width: 12px;
        height: 1.5px;
        background: var(--flow-line);
        flex: none;
      }
      .glyph-box {
        width: 16px;
        height: 16px;
        border: 1.5px dashed var(--flow-line);
        border-radius: var(--radius-xs);
      }
      .template-text {
        min-width: 0;
      }
      .trailing {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: flex-end;
        gap: var(--space-3);
      }
      .trailing button {
        margin-left: auto;
      }
      .template-text h3 {
        margin: 0;
        font: var(--type-body);
        font-weight: 600;
        overflow-wrap: anywhere;
      }
      .template-text p {
        margin: 2px 0 0;
        font: var(--type-small);
        color: var(--muted);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .template-text .template-note {
        white-space: normal;
        font: var(--type-caption);
        font-weight: 400;
        color: var(--warning-ink);
      }
      @media (max-width: 767px) {
        .template-row {
          grid-template-columns: minmax(0, 1fr);
        }
        .glyph {
          display: none;
        }
        .trailing {
          grid-column: 1 / -1;
          justify-content: space-between;
        }
        .template-text p {
          white-space: normal;
        }
      }
    `,
  ],
})
export class TemplateGallery implements OnInit {
  heading = input("Start from a template");
  /** Adds a "Blank workflow" card that emits blank. */
  showBlank = input(false);
  /** Disables every choice, for example while the editor is read-only. */
  disabled = input(false);
  /** No platform: a template opens as a draft kept on this computer. */
  local = input(false);
  choose = output<TemplateChoice>();
  blank = output<void>();

  prefix = `template-gallery-${++sequence}`;
  state = signal<"loading" | "ready" | "error">("loading");
  templates = signal<readonly WorkflowTemplate[]>([]);
  private cdr = inject(ChangeDetectorRef);

  ngOnInit() {
    void this.load();
  }
  async load() {
    this.state.set("loading");
    try {
      this.templates.set(await loadWorkflowTemplates());
      this.state.set("ready");
    } catch {
      this.state.set("error");
    } finally {
      this.cdr.markForCheck();
    }
  }
  pick(template: WorkflowTemplate) {
    if (this.disabled()) return;
    this.choose.emit({
      id: template.id,
      title: template.title,
      yaml: template.yaml,
    });
  }
  /** Up to four step icons: 120 px of 16 px icons joined by 12 px lines. */
  glyph(kinds: readonly Kind[]) {
    return [...new Set(kinds)].slice(0, 4);
  }
  steps(kinds: readonly Kind[]) {
    return kinds.map((kind) => stepKindLabels[kind]).join(", ");
  }
}
