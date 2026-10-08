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
import { execFileSync } from "node:child_process";
import { delimiter, resolve } from "node:path";
import { python, pythonAvailable } from "./python-path";
import { describe, expect, it } from "vitest";
import {
  describeDiagnostic,
  fieldSentence,
  plainCopyCodes,
} from "../src/app/designer/diagnostic-copy";

const compilerCodes = [
  "UNAVAILABLE_REFERENCE",
  "MISSING_REFERENCE",
  "REFERENCE_PRESENCE",
  "INCOMPLETE_BRANCH_OUTPUT",
  "TYPE_MISMATCH",
  "UNKNOWN_COMPATIBILITY",
  "UNKNOWN_ACTION",
  "UNKNOWN_CONNECTOR",
  "UNKNOWN_TASK",
  "UNKNOWN_ADAPTER",
  "UNKNOWN_CONNECTOR_ACTION",
  "UNKNOWN_RESOURCE",
  "CATALOG_PENDING",
  "CONNECTION",
  "DUPLICATE_ID",
  "DUPLICATE_SIGNAL",
  "IMMUTABLE_VERSION",
  "STEP_LIMIT",
  "DEPTH_LIMIT",
  "CONCURRENCY_LIMIT",
  "RESOURCE_LIMIT",
  "ARTIFACT_LIMIT",
  "DIAGNOSTICS_TRUNCATED",
  "CONFIG_CONTRACT",
  "SIDE_EFFECT_CONTRACT",
  "TIMEOUT_CONTRACT",
  "INPUT_CONTRACT",
  "OUTPUT_CONTRACT",
  "ROUTING_CONTRACT",
].map((code) => `WV-COMP-${code}`);

describe("plain-language diagnostic copy", () => {
  it("has specific copy for every compiler code, never the raw code", () => {
    expect(compilerCodes.length).toBeGreaterThanOrEqual(12);
    for (const code of compilerCodes) {
      const view = describeDiagnostic({
        code,
        message: "Definition violates the contract.",
      });
      expect(plainCopyCodes.has(code), code).toBe(true);
      expect(view.text, code).not.toMatch(/WV-|_[A-Z]|violates/);
      expect(view.text, code).toMatch(/^[A-Z].*\.$/);
      expect(view.code).toBe(code);
      expect(view.technical).toBe("Definition violates the contract.");
    }
  });
  it("explains the dominance error in the author's terms", () => {
    const view = describeDiagnostic({
      code: "WV-COMP-UNAVAILABLE_REFERENCE",
      severity: "error",
      message:
        "Referenced step does not dominate this expression in its lexical scope.",
    });
    expect(view.text).toContain("hasn't finished");
    expect(view.severity).toBe("error");
    expect(view.hint).toMatch(/workflow input/);
  });
  it("prefers the server hint and keeps the severity it reports", () => {
    const view = describeDiagnostic({
      code: "WV-COMP-REFERENCE_PRESENCE",
      severity: "warning",
      message:
        "Reference presence is unproved; runtime validation is required.",
      hint: "Mark customerId as required in the input schema.",
    });
    expect(view).toMatchObject({
      severity: "warning",
      hint: "Mark customerId as required in the input schema.",
    });
    const pending = describeDiagnostic({
      code: "WV-COMP-CATALOG_PENDING",
      severity: "info",
      message: "x",
    });
    expect(pending.severity).toBe("info");
    // Local checks report it whether or not a platform is connected.
    expect(pending.text).not.toMatch(/when you connect/);
    expect(
      describeDiagnostic({ code: "WV-COMP-TYPE_MISMATCH", severity: "fatal" })
        .severity,
    ).toBe("error");
  });
  it("says what UNUSED and SCHEMA mean, in the author's words", () => {
    const unused = describeDiagnostic({
      code: "WV-COMP-UNUSED",
      severity: "warning",
      message: "The output of this step is never read.",
    });
    expect(unused.text).toBe("Nothing reads this step's output.");
    expect(unused.hint).toBe(
      "Use it in a later step or the workflow result, or delete the step.",
    );
    const label = (key: string) =>
      ({ durationSeconds: "Duration", timeoutSeconds: "Timeout" })[key] ?? "";
    const schema = describeDiagnostic(
      {
        code: "WV-COMP-SCHEMA",
        message: "durationSeconds must be greater than or equal to 1.",
      },
      label,
    );
    expect(schema.text).toBe("Duration must be at least 1 second.");
    expect(schema.text).not.toBe(
      "Something in this definition needs attention.",
    );
    expect(schema.technical).toBe(
      "durationSeconds must be greater than or equal to 1.",
    );
    expect(
      fieldSentence(
        "timeoutSeconds must be less than or equal to 86400",
        label,
      ),
    ).toBe("Timeout must be at most 86400 seconds.");
    // A sentence about a field Studio doesn't name stays as it is.
    expect(fieldSentence("payload must be an object.", label)).toBe(
      "payload must be an object.",
    );
    // Markup or JSON is never a headline.
    expect(
      describeDiagnostic({ code: "WV-COMP-NEW", message: "{ bad: true }" })
        .text,
    ).toBe("Something in this definition needs attention.");
  });
  it("covers expression, schema and parse families with a safe fallback", () => {
    expect(describeDiagnostic({ code: "WV-EXPR-ARITY" }).text).toMatch(
      /arguments/,
    );
    expect(describeDiagnostic({ code: "WV-SCHEMA-SECRET_VALUE" }).text).toMatch(
      /[Ss]ecret/,
    );
    expect(describeDiagnostic({ code: "WV-PARSE-ALIAS" }).text).toMatch(
      /alias/,
    );
    expect(describeDiagnostic({ code: "WV-PARSE-NEW_THING" }).text).toMatch(
      /YAML or JSON/,
    );
    expect(describeDiagnostic({ code: "WV-EXPR-NEW_THING" }).text).toMatch(
      /expression/,
    );
    expect(describeDiagnostic({ code: "WV-SCHEMA-NEW_THING" }).text).toMatch(
      /schema/,
    );
    const unknown = describeDiagnostic({
      code: "WV-OTHER-THING",
      message: "Server explanation.",
    });
    expect(unknown.text).toBe("Server explanation.");
    expect(describeDiagnostic({}).text).toBe(
      "Something in this definition needs attention.",
    );
    expect(describeDiagnostic({ code: 42, message: { x: 1 } })).toMatchObject({
      code: "",
      technical: "",
    });
  });
});

// Every code the Python compiler can emit must have specific copy.
const root = resolve(import.meta.dirname, "../..");
describe.skipIf(!pythonAvailable())(
  "copy coverage of the Python compiler",
  () => {
    it("knows every WV-COMP code the analyzer and compiler register", () => {
      const codes: string[] = JSON.parse(
        execFileSync(
          python,
          [
            "-c",
            `
import inspect, json, re
from firefly_weave.compiler import analyzer, api, action_config
text = inspect.getsource(analyzer) + inspect.getsource(api)
codes = set(re.findall(r'(?:self\\.issue|issue)\\(\\s*"([A-Z_]+)"', text))
codes |= set(re.findall(r'"(UNKNOWN_[A-Z_]+)"', text))
codes |= set(re.findall(r'WV-COMP-([A-Z_]+)', text))
codes |= set(action_config.ACTION_CONFIG_CODES)
print(json.dumps(sorted("WV-COMP-" + c for c in codes)))
`,
          ],
          {
            cwd: root,
            encoding: "utf8",
            timeout: 180_000,
            env: {
              ...process.env,
              PYTHONPATH: [resolve(root, "src"), process.env.PYTHONPATH]
                .filter(Boolean)
                .join(delimiter),
            },
          },
        ),
      );
      expect(codes.length).toBeGreaterThanOrEqual(20);
      expect(codes.filter((code) => !plainCopyCodes.has(code))).toEqual([]);
    });
  },
);
