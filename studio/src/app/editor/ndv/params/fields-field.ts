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
// A group of fields (an object input, a decision path, Otherwise, branch
// results): its children in order; optional children at their default wait
// behind "+ <label>" buttons at the end of the group. A child shown as a
// trailing card (a decision's Otherwise) ends the cards of the list before
// it, above that list's add button.
import {
  ChangeDetectionStrategy,
  Component,
  forwardRef,
  input,
} from "@angular/core";
import { Icon } from "../../../icon";
import type { ParamSpec } from "../registry";
import type { FieldEntry } from "./form-model";
import type { FormSession } from "./form-session";
import { ParamField } from "./param-field";

@Component({
  selector: "weave-fields-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, forwardRef(() => ParamField)],
  template: `@let parts = session().children(spec());
    @let host = actionHost(parts.shown);
    <div
      class="param-group"
      role="group"
      [attr.aria-labelledby]="labelId() || null"
    >
      @for (
        entry of parts.shown;
        track session().fieldKey(entry);
        let i = $index
      ) {
        @if (!trailing(parts.shown, i - 1)) {
          <weave-param-field
            [session]="session()"
            [spec]="entry.spec"
            [entry]="entryOf(entry)"
            [actions]="
              entry === host && !readOnly() ? addActions(parts.collapsed) : []
            "
          >
            @if (trailing(parts.shown, i); as card) {
              <div
                class="is-card param-card-end"
                role="group"
                ngProjectAs=".param-list-end"
                [attr.aria-label]="card.spec.label"
              >
                <weave-param-field
                  [session]="session()"
                  [spec]="card.spec"
                  [entry]="entryOf(card)"
                  [card]="true"
                >
                  <div class="param-card-head">
                    <span class="param-grip-space" aria-hidden="true"></span>
                    <strong>{{ card.spec.label }}</strong>
                    @if (session().rowInfo(card.spec.path); as info) {
                      <span class="param-card-route">
                        <span class="param-chip-out" [attr.title]="info.chip"
                          ><weave-icon name="switch" [size]="16" /><span
                            class="param-chip-label"
                            >{{ info.chip }}</span
                          ></span
                        >
                        <span class="param-next" [attr.title]="'→ ' + info.next"
                          >→ {{ info.next }}</span
                        >
                      </span>
                    }
                  </div>
                </weave-param-field>
              </div>
            }
          </weave-param-field>
        }
      }
      @if (parts.collapsed.length && !readOnly() && !host) {
        <div class="param-group-add">
          @for (child of parts.collapsed; track child.id) {
            <button
              type="button"
              class="text-link param-add"
              (click)="session().expandChild(child)"
            >
              <weave-icon name="plus" [size]="16" />{{ child.label }}
            </button>
          }
        </div>
      }
    </div>`,
})
export class FieldsField {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  labelId = input("");
  readOnly = input<string | null>(null);
  private actionCache: {
    session: FormSession;
    children: string;
    actions: { label: string; run: () => void }[];
  } | null = null;

  /** The entry as shown: read-only throughout while the group is. */
  entryOf(entry: FieldEntry): FieldEntry {
    const reason = this.readOnly();
    return reason
      ? {
          spec: entry.spec,
          option: false,
          readOnly: reason,
          disabled: entry.disabled,
        }
      : entry;
  }
  /**
   * The trailing card that follows the list at `index`: a group of fields
   * shown as one more card of that list, above its add button.
   */
  trailing(shown: FieldEntry[], index: number): FieldEntry | null {
    const list = shown[index];
    const next = shown[index + 1];
    return list?.spec.type === "list" &&
      list.spec.item?.type === "fields" &&
      next?.spec.type === "fields" &&
      next.spec.trailingCard
      ? next
      : null;
  }
  /**
   * A path's conditions end with their own add line: the group's options
   * join it ("Add condition", "Path result") instead of a line of their own.
   */
  actionHost(shown: FieldEntry[]): FieldEntry | null {
    const last = shown.at(-1);
    return last?.spec.type === "conditions" ? last : null;
  }
  addActions(collapsed: ParamSpec[]): { label: string; run: () => void }[] {
    const session = this.session();
    const children = JSON.stringify(
      collapsed.map((child) => [child.id, child.path]),
    );
    if (
      this.actionCache?.session !== session ||
      this.actionCache.children !== children
    )
      this.actionCache = {
        session,
        children,
        actions: collapsed.map((child) => ({
          label: child.label,
          run: () => session.expandChild(child),
        })),
      };
    return this.actionCache.actions;
  }
}
