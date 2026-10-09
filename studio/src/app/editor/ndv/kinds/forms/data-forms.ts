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
// Transform and Decision table. A transform builds fields from Fixed or
// Mapped values; a decision table maps its input row by row, or as a whole.
import type { Step } from "../../../../model";
import { paramsFromSchema } from "../../params/schema-params";
import type { FormSpec, KindContext, NdvContext } from "../../registry";
import { isRecord, schemaOf, specOf } from "../shared";
import { wholeMapping } from "./shared";

const literalObject = (value: unknown): boolean =>
  value === undefined ||
  (isRecord(value) &&
    ("object" in value || ("literal" in value && isRecord(value["literal"]))));

export const transformForm = (step: Step): FormSpec => ({
  fields: [
    literalObject(step["value"]) || wholeMapping(step["value"])
      ? {
          id: "value",
          path: ["value"],
          type: "keyValue",
          label: "Fields",
          mapping: "both",
          default: {},
          whenRemoved: "default",
          addLabel: "Add field",
          hint: "Drag input fields here, or add a field.",
        }
      : {
          id: "value-any",
          path: ["value"],
          type: "json",
          label: "Fields",
          mapping: "both",
          default: {},
          whenRemoved: "default",
          hint: "This value isn't a set of fields; edit it as JSON or map it.",
        },
  ],
});

export function decisionTableForm(step: Step, ctx: KindContext): FormSpec {
  const uses = String(step["uses"] ?? "");
  const input = schemaOf(
    specOf(uses ? ctx.tableContract(uses) : null)["inputSchema"],
  );
  const table = {
    id: "table",
    path: ["uses"],
    type: "resource" as const,
    label: "Table",
    required: true,
    placeholder: "payment-policy@1.0.0",
    hint: "The workflow uses this exact published version.",
    choices: (ndv: NdvContext) =>
      ndv.catalog
        .tables()
        .then((rows) =>
          rows.map((row) => ({ value: row.uses, label: row.title })),
        ),
  };
  if (!input) return { fields: [table] };
  const rows = paramsFromSchema(input, ["with"], { idPrefix: "input." });
  if (wholeMapping(step["with"]))
    return {
      fields: [
        table,
        {
          id: "input",
          path: ["with"],
          type: "fields",
          label: "Table input",
          mapping: "both",
          default: {},
          whenRemoved: "default",
          hint: "The whole input is mapped. Switch to Fixed to fill it field by field.",
          children: () => rows.fields,
        },
      ],
    };
  return { fields: [table, ...rows.fields], options: rows.options };
}
