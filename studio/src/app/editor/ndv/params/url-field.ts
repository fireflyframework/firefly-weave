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
// The URL of Call an API: the connection's address as a read-only prefix,
// then the path. At rest each {name} placeholder shows as a pill of the data
// that fills it; focused, or while data is dragged over it, the path is
// plain text to type in or drop at. Each placeholder has a row below for its
// value, Fixed or Mapped.
import {
  ChangeDetectionStrategy,
  Component,
  forwardRef,
  input,
  signal,
} from "@angular/core";
import { canonicalJson } from "../../../forms/core/json";
import type { ParamSpec } from "../registry";
import type { FieldEntry } from "./form-model";
import type { FormSession } from "./form-session";
import { RefPill } from "./formula-field";
import { ParamField } from "./param-field";
import { urlParts, type UrlPart } from "./url-template";
import type { ParamValue } from "./value-io";

@Component({
  selector: "weave-url-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [RefPill, forwardRef(() => ParamField)],
  template: `@let view = parts();
    @let pills = !editing() && hasPlaceholder(view);
    <div class="param-url" [class.has-view]="pills">
      <span class="param-url-prefix" [id]="controlId() + '-prefix'"
        >Connection address</span
      >
      <span class="param-url-path" [attr.title]="pills ? path() : null">
        <input
          class="param-input param-url-input"
          autocomplete="off"
          spellcheck="false"
          [id]="controlId()"
          [attr.aria-describedby]="describedIds()"
          [attr.aria-required]="spec().required || null"
          [attr.aria-invalid]="invalid() || null"
          [attr.aria-readonly]="!!readOnly() || null"
          [readOnly]="!!readOnly()"
          [placeholder]="spec().placeholder ?? ''"
          [value]="path()"
          (focus)="begin()"
          (input)="typed($event)"
          (blur)="end()"
        />
        @if (pills) {
          <span class="param-url-view" aria-hidden="true">
            @for (part of view; track $index) {
              @if (isText(part)) {
                <span class="param-url-text">{{ part.text }}</span>
              } @else {
                @let ref = refOf(nameOf(part));
                @if (ref) {
                  <span class="param-url-brace">{{ "{" }}</span
                  ><weave-ref-pill
                    [pointer]="ref"
                    [breadcrumb]="breadcrumb(ref)"
                    [icon]="iconOf(ref)"
                  /><span class="param-url-brace">{{ "}" }}</span>
                } @else {
                  <span class="param-url-slot">{{
                    "{" + nameOf(part) + "}"
                  }}</span>
                }
              }
            }
          </span>
        }
      </span>
    </div>
    @if (session().urlPartsReason(); as reason) {
      <p class="param-deferred">{{ reason }}</p>
    } @else if (partSpecs().length) {
      <div
        class="param-url-parts"
        role="group"
        [attr.aria-label]="'Path values of ' + spec().label"
      >
        @for (part of partSpecs(); track part.id) {
          <div class="param-url-part">
            <span class="param-url-key" aria-hidden="true">{{
              "{" + part.label + "}"
            }}</span>
            <weave-param-field
              class="param-row-value"
              [session]="session()"
              [spec]="part"
              [entry]="entry(part)"
              [row]="true"
            />
          </div>
        }
      </div>
    }`,
})
export class UrlField {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  controlId = input.required<string>();
  describedBy = input("");
  readOnly = input<string | null>(null);
  invalid = input(false);
  readonly editing = signal(false);
  /** The edit in progress: whose path it is, what it last wrote, and the values to keep in step. */
  private edit: {
    session: FormSession;
    descriptor: string;
    written: string;
    snapshot: Map<string, ParamValue>;
  } | null = null;

  path(): string {
    return this.session().urlPath(this.spec());
  }
  parts(): UrlPart[] {
    return urlParts(this.path());
  }
  isText(part: UrlPart): part is { text: string } {
    return "text" in part;
  }
  nameOf(part: UrlPart): string {
    return "name" in part ? part.name : "";
  }
  hasPlaceholder(parts: UrlPart[]): boolean {
    return parts.some((part) => !this.isText(part));
  }
  describedIds(): string {
    return `${this.controlId()}-prefix ${this.describedBy()}`.trim();
  }
  partSpecs(): ParamSpec[] {
    return this.session().urlParts(this.spec());
  }
  entry(part: ParamSpec): FieldEntry {
    const entry = this.session().childEntry(part);
    return { ...entry, readOnly: this.readOnly() ?? entry.readOnly };
  }
  /** The data a placeholder's value maps, when it is one reference. */
  refOf(name: string): string | null {
    const part = this.partSpecs().find((spec) => spec.path[2] === name);
    const value = part ? this.session().read(part) : null;
    if (value?.mode !== "mapped") return null;
    const expression = value.expression as Record<string, unknown>;
    return Object.keys(expression).length === 1 &&
      typeof expression["ref"] === "string"
      ? expression["ref"]
      : null;
  }
  private entryOf(ref: string) {
    const part = this.partSpecs()[0];
    return part
      ? this.session()
          .scope(part)
          .find((entry) => entry.ref === ref)
      : undefined;
  }
  breadcrumb(ref: string): string {
    return (this.entryOf(ref)?.breadcrumb ?? ref).replace(
      /^Workflow input(?= ›|$)/,
      "Input",
    );
  }
  iconOf(ref: string): string {
    const entry = this.entryOf(ref);
    return entry?.source === "step" ? (entry.stepKind ?? "action") : "source";
  }

  begin() {
    this.editing.set(true);
    const session = this.session();
    const spec = this.spec();
    this.edit = {
      session,
      descriptor: canonicalJson(spec),
      written: session.urlPath(spec),
      snapshot: session.urlSnapshot(spec),
    };
  }
  typed(event: Event) {
    const text = (event.target as HTMLInputElement).value;
    const session = this.session();
    const spec = this.spec();
    // Another path (a new field, Undo, another step) starts a new edit.
    if (
      !this.edit ||
      this.edit.session !== session ||
      this.edit.descriptor !== canonicalJson(spec) ||
      this.edit.written !== session.urlPath(spec)
    )
      this.begin();
    const edit = this.edit!;
    if (session.typeUrl(spec, text, edit.snapshot)) edit.written = text;
  }
  end() {
    this.editing.set(false);
    this.edit = null;
  }
}
