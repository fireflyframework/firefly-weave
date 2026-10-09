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
// behind "+ <label>" buttons at the end of the group.
import {
  ChangeDetectionStrategy,
  Component,
  forwardRef,
  input,
} from "@angular/core";
import { Icon } from "../../../icon";
import type { ParamSpec } from "../registry";
import type { FormSession } from "./form-session";
import { ParamField } from "./param-field";

@Component({
  selector: "weave-fields-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, forwardRef(() => ParamField)],
  template: `@let parts = session().children(spec());
    <div
      class="param-group"
      role="group"
      [attr.aria-labelledby]="labelId() || null"
    >
      @for (entry of parts.shown; track session().fieldKey(entry)) {
        <weave-param-field
          [session]="session()"
          [spec]="entry.spec"
          [entry]="
            readOnly()
              ? {
                  spec: entry.spec,
                  option: false,
                  readOnly: readOnly(),
                  disabled: entry.disabled,
                }
              : entry
          "
        />
      }
      @if (parts.collapsed.length && !readOnly()) {
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
}
