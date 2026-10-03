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
// Plain-language explanations of compiler diagnostics. Rows show `text`
// (and `hint`); the stable code and the compiler's own message stay
// available as secondary detail for support.
import { readable } from "../errors";

export type Severity = "error" | "warning" | "info";

export interface DiagnosticView {
  severity: Severity;
  /** What is wrong, in the author's terms. */
  text: string;
  /** What to do next: the compiler's hint when it gives one. */
  hint?: string;
  /** Stable support code, e.g. `WV-COMP-TYPE_MISMATCH`; "" when absent. */
  code: string;
  /** The compiler's original message, for a "Details" disclosure. */
  technical: string;
}

interface Copy {
  text: string;
  hint?: string;
}

const compiler: Record<string, Copy> = {
  LLM_PROFILE: {
    text: "This AI profile is missing or invalid.",
    hint: "Choose a workflow AI profile, or configure its model and result fields.",
  },
  LLM_ACTION: {
    text: "This action does not provide the required AI worker contract.",
    hint: "Choose the published weave-agentic-generate action and its required connection.",
  },
  DECISION_EMPTY_OUTPUT: {
    text: "A Collect table must allow an empty list as its result.",
    hint: "Remove a minimum item count or another constraint that rejects no matches.",
  },
  UNAVAILABLE_REFERENCE: {
    text: "This uses data from a step that hasn't finished at this point in the workflow.",
    hint: "Use the workflow input or a step that runs earlier on the same path.",
  },
  MISSING_REFERENCE: {
    text: "This refers to a field that the data doesn't have.",
    hint: "Check the field name, or pick the field from the suggestions.",
  },
  REFERENCE_PRESENCE: {
    text: "This field might be missing when the workflow runs, and the run would stop here.",
    hint: "Make the field required where it comes from, or provide a fallback value.",
  },
  INCOMPLETE_BRANCH_OUTPUT: {
    text: "Not every path of the decision produces this field, so it may be missing here.",
    hint: "Set the field in the output of every case and the default.",
  },
  TYPE_MISMATCH: {
    text: "This value's type doesn't match what the field expects.",
    hint: "Choose data of the expected type, or convert it in a Transform step first.",
  },
  UNKNOWN_COMPATIBILITY: {
    text: "Studio can't confirm that this value fits the field. It's checked again when the workflow runs.",
  },
  UNKNOWN_ACTION: {
    text: "This action isn't published in the project.",
    hint: "Check its name and version, or publish the action first.",
  },
  UNKNOWN_CONNECTOR: {
    text: "This connector isn't available in the project yet.",
    hint: "Ask an operator to publish the connector for this project.",
  },
  UNKNOWN_TASK: {
    text: "No worker task with this type and version is registered in the project.",
  },
  UNKNOWN_ADAPTER: {
    text: "The connector's adapter isn't installed on this platform.",
    hint: "Ask an operator to install the connector package.",
  },
  UNKNOWN_CONNECTOR_ACTION: {
    text: "The connector doesn't offer this operation.",
  },
  UNKNOWN_RESOURCE: {
    text: "Something this definition depends on isn't in the project catalog.",
  },
  CATALOG_PENDING: {
    text: "This is checked against the project catalog when you validate while connected to a platform.",
  },
  CONNECTION: {
    text: "This step's connection doesn't match what the action needs.",
    hint: "Choose a connection slot that uses the action's connector.",
  },
  DUPLICATE_ID: {
    text: "Another step already uses this ID.",
    hint: "Give each step a unique ID.",
  },
  DUPLICATE_SIGNAL: {
    text: "Another step already waits for a signal with this name.",
    hint: "Give each signal step a unique signal name.",
  },
  IMMUTABLE_VERSION: {
    text: "This version is already published with different content.",
    hint: "Change the version number before publishing.",
  },
  STEP_LIMIT: { text: "The workflow has more steps than the platform allows." },
  DEPTH_LIMIT: {
    text: "Branches are nested deeper than the platform allows.",
  },
  CONCURRENCY_LIMIT: {
    text: "The parallel limit is higher than the platform allows.",
  },
  RESOURCE_LIMIT: {
    text: "This part of the definition is too large or complex to check.",
    hint: "Split it into smaller steps or simplify the schema.",
  },
  ARTIFACT_LIMIT: {
    text: "The compiled workflow is larger than the platform allows.",
  },
  DIAGNOSTICS_TRUNCATED: {
    text: "There are more problems than can be listed.",
    hint: "Fix the problems shown, then validate again.",
  },
  CONFIG_CONTRACT: {
    text: "The action's connector settings don't match what the connector accepts.",
  },
  SIDE_EFFECT_CONTRACT: {
    text: "The action's side effect doesn't match its connector operation.",
  },
  TIMEOUT_CONTRACT: {
    text: "The timeout is longer than the connector allows.",
  },
  INPUT_CONTRACT: {
    text: "The action's input fields don't match its connector operation.",
  },
  OUTPUT_CONTRACT: {
    text: "The action's result fields don't match its connector operation.",
  },
  ROUTING_CONTRACT: {
    text: "The worker routing doesn't match the action's task.",
  },
  UNUSED: {
    text: "Nothing reads this step's output.",
    hint: "Use it in a later step or the workflow result, or delete the step.",
  },
};

const expression: Record<string, Copy> = {
  INVALID: { text: "This expression isn't valid." },
  TYPE: {
    text: "An operation in this expression gets a value of the wrong type.",
  },
  ARITY: { text: "An operation has the wrong number of arguments." },
  MISSING: { text: "This expression refers to data that doesn't exist." },
  INVALID_JSON: { text: "This value isn't valid JSON data." },
  RESOURCE_LIMIT: { text: "This expression is too large." },
};

const schema: Record<string, Copy> = {
  INVALID_INSTANCE: {
    text: "This value doesn't have the expected format.",
  },
  INVALID_SCHEMA: { text: "This schema isn't valid." },
  INVALID_FORMAT: { text: "This value doesn't match its declared format." },
  UNSUPPORTED_KEYWORD: {
    text: "This schema uses a keyword that Weave doesn't support.",
  },
  UNSUPPORTED_FORMAT: {
    text: "This schema uses a format that Weave doesn't support.",
  },
  UNSUPPORTED_PATTERN: {
    text: "This pattern uses regular-expression features that Weave doesn't support.",
  },
  UNSUPPORTED_DIALECT: {
    text: "This schema uses a JSON Schema version that Weave doesn't support.",
  },
  INVALID_REF: {
    text: "This schema points to a definition that doesn't exist.",
  },
  RECURSIVE_REF: { text: "Schemas can't refer to themselves." },
  SECRET_VALUE: {
    text: "Secret values can't be stored in a workflow.",
    hint: "Leave secrets to the connection's credentials.",
  },
  CLASSIFICATION: {
    text: "This field's data classification isn't allowed here.",
  },
  RESOURCE_LIMIT: { text: "This schema is too large or complex to check." },
  DIAGNOSTICS_TRUNCATED: {
    text: "There are more schema problems than can be listed.",
  },
};

const parse: Record<string, Copy> = {
  ALIAS: { text: "YAML aliases aren't allowed in definitions." },
  MERGE_KEY: { text: "YAML merge keys aren't allowed in definitions." },
  TAG: { text: "Custom YAML tags aren't allowed in definitions." },
  EXTRA_DOCUMENT: {
    text: "The source contains more than one document.",
  },
  ROOT: { text: "The source must be a single object." },
  NON_STRING_KEY: { text: "Every key must be text." },
  SOURCE_LIMIT: { text: "The source is too large." },
  DEPTH_LIMIT: { text: "The source is nested too deeply." },
  NODE_LIMIT: { text: "The source has too many values." },
  BOM: { text: "Remove the byte-order mark at the start of the source." },
  ENCODING: { text: "The source must be UTF-8 text." },
};

const families: Record<string, { copy: Record<string, Copy>; fallback: Copy }> =
  {
    COMP: {
      copy: compiler,
      fallback: { text: "Something in this definition needs attention." },
    },
    EXPR: {
      copy: expression,
      fallback: { text: "This expression can't be evaluated." },
    },
    DECISION: {
      copy: {
        REFERENCE: {
          text: "A decision rule can only read the table input.",
          hint: "Pass workflow data into the table, then choose it from Input data.",
        },
        MULTIPLE_MATCHES: {
          text: "More than one rule matched a Unique table.",
          hint: "Make the rules mutually exclusive, or choose First or Collect.",
        },
        NO_MATCH: {
          text: "No decision rule matched and there is no default result.",
          hint: "Add a default result or a rule for this input.",
        },
        PREDICATE: { text: "A rule condition must produce true or false." },
        INPUT: {
          text: "The data passed to this table does not match its input fields.",
        },
        OUTPUT: {
          text: "The decision result does not match its declared output fields.",
        },
        CONTRACT: { text: "The decision table is incomplete or invalid." },
        RESOURCE_LIMIT: {
          text: "The decision table exceeds an evaluation limit.",
        },
      },
      fallback: { text: "This decision table could not be evaluated." },
    },
    SCHEMA: {
      copy: schema,
      fallback: {
        text: "This value or schema doesn't fit the expected schema.",
      },
    },
    PARSE: {
      copy: parse,
      fallback: {
        text: "The source can't be read. Check the YAML or JSON syntax.",
      },
    },
  };

/** Every code with specific (not fallback) copy. */
export const plainCopyCodes: ReadonlySet<string> = new Set(
  Object.entries(families).flatMap(([family, { copy }]) =>
    Object.keys(copy).map((code) => `WV-${family}-${code}`),
  ),
);

const text = (value: unknown) =>
  typeof value === "string" && value.trim() ? value.trim() : "";

/** Words for a schema bound: "at least", "at most", "more than", "less than". */
const bounds: [RegExp, string][] = [
  [/must be greater than or equal to/i, "must be at least"],
  [/must be less than or equal to/i, "must be at most"],
  [/must be greater than/i, "must be more than"],
  [/must be less than/i, "must be less than"],
];

/**
 * A compiler sentence about one field in the author's words:
 * "durationSeconds must be greater than or equal to 1." becomes "Duration
 * must be at least 1 second." `label` names a field key ("" when unknown).
 */
export function fieldSentence(
  message: string,
  label: (key: string) => string = () => "",
): string {
  const match = /^([A-Za-z_$][\w$]*)\s+(.+?)\.?$/.exec(message.trim());
  if (!match) return message;
  const [, key, rest] = match;
  const name = label(key);
  if (!name) return message;
  let tail = rest;
  for (const [pattern, words] of bounds) tail = tail.replace(pattern, words);
  // Duration fields store seconds: name the unit.
  const amount =
    /^(must be (?:at least|at most|more than|less than)) (-?\d+(?:\.\d+)?)$/.exec(
      tail,
    );
  if (amount && /Seconds$/.test(key)) {
    const n = Number(amount[2]);
    tail = `${amount[1]} ${amount[2]} ${n === 1 ? "second" : "seconds"}`;
  }
  return `${name} ${tail}.`;
}

export function describeDiagnostic(
  diagnostic: {
    code?: unknown;
    message?: unknown;
    severity?: unknown;
    hint?: unknown;
  },
  /** The plain name of a field key, such as "Duration" for durationSeconds. */
  label?: (key: string) => string,
): DiagnosticView {
  const code = text(diagnostic.code);
  const technical = text(diagnostic.message);
  const severity: Severity =
    diagnostic.severity === "warning" || diagnostic.severity === "info"
      ? diagnostic.severity
      : "error";
  const match = /^WV-([A-Z0-9]+)-([A-Z0-9_]+)$/.exec(code);
  const family = match ? families[match[1]] : undefined;
  const specific = family ? family.copy[match![2]] : undefined;
  // Without specific copy, a readable compiler sentence says more than the
  // family's general line: it becomes the headline, in the author's words.
  const copy: Copy = specific
    ? specific
    : readable(technical)
      ? { text: fieldSentence(technical, label) }
      : (family?.fallback ?? {
          text: technical || "Something in this definition needs attention.",
        });
  const hint = text(diagnostic.hint) || copy.hint;
  return {
    severity,
    text: copy.text,
    code,
    technical,
    ...(hint ? { hint } : {}),
  };
}
