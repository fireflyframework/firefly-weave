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
// A schema field (a trigger's input, a signal's payload, a human task's
// form, a result schema): the shared schema designer with Paste sample JSON.
// The schema's JSON stays in Edit as YAML, so values such as a write-only
// field's default never show in step details.
// Fields read from an example arrive only while the field they were pasted
// into is still the one on screen, unchanged.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  inject,
  input,
} from "@angular/core";
import type { JsonObject } from "../../../forms/core/json";
import {
  SchemaDesigner,
  type DesignerPurpose,
  type InferSchema,
} from "../../../forms/ui/schema-designer";
import type { ParamSpec } from "../registry";
import type { FormSession } from "./form-session";
import { designerSchema } from "./schema-params";

export const STALE_EXAMPLE =
  "The step changed while Studio read the example. Paste it again.";

/**
 * Inference whose answer counts only if the field is still current when it
 * arrives: `start()` is called as the example is sent and returns the check.
 */
export function guardedInference(
  run: InferSchema,
  start: () => () => boolean,
): InferSchema {
  return async (samples) => {
    const current = start();
    const result = await run(samples);
    if (!current()) throw Error(STALE_EXAMPLE);
    return result;
  };
}

const PURPOSES: Record<string, DesignerPurpose> = {
  inputSchema: "input",
  payload: "payload",
  form: "form",
  resultSchema: "output",
  result: "output",
};

@Component({
  selector: "weave-schema-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [SchemaDesigner],
  template: `<weave-schema-designer
    [heading]="spec().label"
    [schema]="schema()"
    [purpose]="purpose()"
    [readonly]="!!readOnly()"
    [inferSchema]="infer()"
    [preview]="false"
    [compact]="true"
    [json]="false"
    (schemaChange)="changed($event)"
  />`,
})
export class SchemaField {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  readOnly = input<string | null>(null);
  private readonly lifetime = inject(DestroyRef);
  private inference: {
    session: FormSession;
    run: InferSchema;
    infer: InferSchema;
  } | null = null;

  schema(): unknown {
    const value = this.session().read(this.spec());
    return designerSchema(
      value.mode === "fixed" ? value.value : undefined,
      this.spec().default,
    );
  }
  purpose(): DesignerPurpose {
    return PURPOSES[this.spec().id] ?? "other";
  }
  /** The host's inference, refusing an answer that arrives after the field, step or account changed. */
  infer(): InferSchema | null {
    const session = this.session();
    const run = session.host.inferSchema;
    if (!run || this.readOnly()) return null;
    if (this.inference?.session !== session || this.inference.run !== run)
      this.inference = {
        session,
        run,
        infer: guardedInference(run, () => {
          const owns = session.owns(this.spec());
          return () =>
            !this.lifetime.destroyed && this.session() === session && owns();
        }),
      };
    return this.inference.infer;
  }
  changed(schema: JsonObject) {
    this.session().write(this.spec(), { mode: "fixed", value: schema });
  }
}
