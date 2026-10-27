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
// The decision table grid, in the wide layout of step details. It edits the
// step's table: the published version it reads, or the draft of its next
// version kept on this computer.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import { Icon } from "../../icon";
import type { NdvContext, ParameterComponent } from "../ndv/registry";

const WHERE: Record<string, string> = {
  draft: "on this computer",
  copy: "on this computer",
  published: "published",
};

@Component({
  selector: "weave-decision-grid",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  template: `
    <section class="dg" [attr.aria-label]="'Table ' + title()">
      <header class="dg-head">
        <button
          type="button"
          class="text-link dg-back"
          (click)="context().closeParameterComponent?.()"
        >
          <weave-icon name="chevronLeft" [size]="16" />Back to the step
        </button>
        <h3 class="dg-title">{{ title() }}</h3>
      </header>
    </section>
  `,
})
export class DecisionGrid implements ParameterComponent {
  readonly context = input.required<NdvContext>();
  private uses(): string {
    const uses = this.context().step["uses"];
    return typeof uses === "string" ? uses : "";
  }
  /** "payment-policy 1.2.1 · on this computer". */
  title(): string {
    const uses = this.uses();
    const at = uses.lastIndexOf("@");
    const where = this.context().tables?.where(uses);
    const name = at > 0 ? `${uses.slice(0, at)} ${uses.slice(at + 1)}` : uses;
    return where ? `${name} · ${WHERE[where]}` : name;
  }
}
