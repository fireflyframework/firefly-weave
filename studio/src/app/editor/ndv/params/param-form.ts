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
// The Parameters or Settings tab of step details: every shown field in
// order. Add option joins it in the next change.
import {
  ChangeDetectionStrategy,
  Component,
  ViewEncapsulation,
  input,
} from "@angular/core";
import type { FormName, FormSession } from "./form-session";
import { canonicalJson } from "../../../forms/core/json";
import type { FieldEntry } from "./form-model";
import { AddOption } from "./add-option";
import { ParamField } from "./param-field";

@Component({
  selector: "weave-parameter-form",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  encapsulation: ViewEncapsulation.None,
  imports: [ParamField, AddOption],
  styleUrl: "./params.css",
  template: `@let state = session().state(name());
    <div class="param-form">
      @for (entry of state.fields; track fieldKey(entry)) {
        <weave-param-field
          [session]="session()"
          [spec]="entry.spec"
          [entry]="entry"
        />
      }
      @if (state.addable.length && !session().controller.readOnlyReason()) {
        <weave-add-option [session]="session()" [options]="state.addable" />
      }
    </div>`,
})
export class ParameterForm {
  session = input.required<FormSession>();
  name = input<FormName>("parameters");
  private owner: FormSession | null = null;
  private formName: FormName | null = null;
  private keys = new Map<
    string,
    {
      descriptor: string;
      key: object;
    }
  >();
  fieldKey(entry: FieldEntry): object {
    if (this.owner !== this.session() || this.formName !== this.name()) {
      this.owner = this.session();
      this.formName = this.name();
      this.keys.clear();
    }
    const spec = entry.spec;
    const descriptor = canonicalJson({
      spec,
      readOnly: entry.readOnly,
      disabled: entry.disabled,
    });
    let cached = this.keys.get(spec.id);
    if (!cached || cached.descriptor !== descriptor) {
      cached = {
        descriptor,
        key: {},
      };
      this.keys.set(spec.id, cached);
    }
    return cached.key;
  }
}
