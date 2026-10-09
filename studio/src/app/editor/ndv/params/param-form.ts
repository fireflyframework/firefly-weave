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
import { ParamField } from "./param-field";

@Component({
  selector: "weave-parameter-form",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  encapsulation: ViewEncapsulation.None,
  imports: [ParamField],
  styleUrl: "./params.css",
  template: `@let state = session().state(name());
    <div class="param-form">
      @for (entry of state.fields; track entry.spec.id) {
        <weave-param-field
          [session]="session()"
          [spec]="entry.spec"
          [entry]="entry"
        />
      }
    </div>`,
})
export class ParameterForm {
  session = input.required<FormSession>();
  name = input<FormName>("parameters");
}
