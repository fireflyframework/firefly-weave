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
// What Studio's editor says about transactions uses the product's words:
// never the engine's names for the parts of a transaction. The platform's own
// values ("compensate") stay in code; only text people read is checked.
import "@angular/compiler";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { beforeAll, describe, expect, it } from "vitest";
import { buildCatalog } from "../src/app/editor/add-step/entries";
import { shown, startState } from "../src/app/editor/add-step/panel-model";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { catalogInput, entryById } from "./add-step-fixtures";
import { peopleStrings } from "./support/people-strings";

beforeAll(() => loadKindRegistrations());

const EDITOR = "src/app/editor";
const ENTRIES = join(EDITOR, "add-step/entries.ts");
const KEYWORDS =
  "transaction undo rollback all or nothing together reserve confirm";

function tree(path: string): string[] {
  if (!existsSync(path)) return [];
  if (!statSync(path).isDirectory()) return [path];
  return readdirSync(path).flatMap((name) => tree(join(path, name)));
}
/** Every editor module about transactions. */
const sources = () =>
  [
    "transactions",
    "ndv/kinds/transaction.ts",
    "ndv/kinds/forms/transaction-forms.ts",
    "ndv/settings/undo-section.ts",
    "canvas/transaction-frame.ts",
    "runs-tab/run-transactions.ts",
  ].flatMap((path) => tree(join(EDITOR, path)));

const ENGINE =
  /saga|unit of work|atomic|\bTCC\b|pivot|compensat|committing|rolled back|rollback|handler|outbox|terminate|accept reconciled|\bnodes?\b|\bNDV\b|\bcredentials?\b|\bexecutions?\b|\bexpressions?\b|\bBPMN\b/i;
// "try 3 of 10" counts attempts; "try" never names a step.
const TRY = /\btry\b(?! (?:\d+|X) of (?:\d+|X)\b)/i;
const banned = (text: string) => ENGINE.test(text) || TRY.test(text);

describe("the editor's transaction words", () => {
  it("never name the engine's parts of a transaction", () => {
    const read = sources().flatMap((file) =>
      peopleStrings(file, readFileSync(file, "utf8")).map((text) => ({
        file,
        text,
      })),
    );
    expect(
      read
        .filter(({ text }) => banned(text))
        .map(({ file, text }) => `${file}: ${text}`),
    ).toEqual([]);
    // The scan reads the modules it is for, and finds what it looks for.
    const probe = [
      'import { x } from "./compensation";',
      'type S = "compensating" | "committing";',
      'const codes = { "WV-TXN-AFTER-PIVOT-STUCK": "A step after the point of no return couldn\'t finish." };',
      'if (mode === "compensate") label = "Compensate the step";',
      "const row = `Undo of ${step} (try ${attempt} of ${of})`;",
      'const verbs = ["Try the step", "Max tries"];',
      'const template = `<p title="Rollback">Undo {{ step }}</p><b>Saga</b>`;',
    ].join("\n");
    expect(peopleStrings("probe.ts", probe).filter(banned)).toEqual([
      "Compensate the step",
      "Try the step",
      "Rollback",
      "Saga",
    ]);
    expect(read.map(({ text }) => text)).toEqual(
      expect.arrayContaining([
        "Still reserved",
        "Run steps together; if one fails, undo what's done",
        "A transaction can't contain another transaction.",
      ]),
    );
  });

  it("say nothing of the engine in Add a step, whatever disables the entry", () => {
    for (const extra of [
      {
        language: {
          features: ["flow.transaction"],
          pending: new Set<string>(),
        },
      },
      { language: { features: [], pending: new Set<string>() } },
      {
        language: {
          features: ["flow.transaction"],
          pending: new Set<string>(),
        },
        insideTransaction: true,
      },
      {
        language: {
          features: ["flow.transaction"],
          pending: new Set(["transaction"]),
        },
      },
    ]) {
      const entry = entryById(
        buildCatalog(catalogInput(extra)),
        "kind:transaction",
      );
      for (const text of [entry.label, entry.description, entry.disabled ?? ""])
        expect(banned(text), text).toBe(false);
    }
  });

  it("keep rollback a hidden word that only finds the entry", () => {
    const catalog = buildCatalog(
      catalogInput({
        language: { features: ["flow.transaction"], pending: new Set() },
      }),
    );
    expect(entryById(catalog, "kind:transaction").keywords).toContain(KEYWORDS);
    expect(
      peopleStrings(ENTRIES, readFileSync(ENTRIES, "utf8")).filter((text) =>
        /rollback/i.test(text),
      ),
    ).toEqual([KEYWORDS]);
    const flow = { ...startState("next"), stack: ["next", "category:flow"] };
    for (const state of [flow, { ...startState("next"), query: "rollback" }]) {
      const view = shown(state, catalog, false);
      expect(view.options.map((entry) => entry.id)).toContain(
        "kind:transaction",
      );
      for (const text of [
        view.note,
        ...view.options.flatMap((entry) => [
          entry.label,
          entry.description,
          entry.disabled ?? "",
          entry.meta,
        ]),
      ])
        expect(text).not.toMatch(/rollback/i);
    }
  });
});
