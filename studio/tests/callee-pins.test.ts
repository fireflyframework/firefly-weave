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
// Called workflows in the activation dialog: the workflows a definition
// calls, found as the platform finds them (the corpus is shared with
// tests/unit/test_called_references_corpus.py), and the activation of each
// that the new activation pins, as the CLI's --pin-current-calls picks it.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  callPins,
  calledReferences,
  calledReferenceSites,
  proposePins,
  type CalleeActivation,
  type CalleeVersion,
} from "../src/app/editor/activation/callee-pins";

const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/called-references-corpus.json"),
    "utf8",
  ),
) as { cases: { name: string; document: unknown; references: string[] }[] };
const byName = (name: string) =>
  corpus.cases.find((item) => item.name === name)!.document;

describe("finding the workflows a definition calls", () => {
  it.each(corpus.cases)(
    "finds what the platform finds: $name",
    ({ document, references }) => {
      expect(calledReferences(document)).toEqual(references);
    },
  );

  it("knows where each call is, as the compiler's diagnostics point to it", () => {
    expect(
      calledReferenceSites(byName("loops, transactions and error paths")),
    ).toEqual([
      { reference: "risky@1.0.0", path: "/spec/steps/2/uses" },
      { reference: "per-item@1.0.0", path: "/spec/steps/0/body/steps/0/uses" },
      {
        reference: "confirm-charge@1.0.0",
        path: "/spec/steps/1/steps/0/confirm/uses",
      },
      {
        reference: "cancel-charge@1.0.0",
        path: "/spec/steps/1/steps/0/cancel/uses",
      },
      {
        reference: "refund@1.0.0",
        path: "/spec/steps/1/steps/0/compensate/uses",
      },
      {
        reference: "recover@1.0.0",
        path: "/spec/steps/2/errorPath/steps/0/uses",
      },
    ]);
    expect(
      calledReferenceSites(byName("parallel branches in name order"))[0],
    ).toEqual({
      reference: "slash@1.0.0",
      path: "/spec/steps/0/branches/a~1b/steps/0/uses",
    });
    expect(
      calledReferenceSites(byName("agent tools that call workflows"))[1],
    ).toEqual({
      reference: "escalate@2.1.0",
      path: "/spec/steps/0/tools/4/workflow",
    });
  });
});

describe("proposing the activation each call pins", () => {
  const versions: CalleeVersion[] = [
    { id: "v-1", name: "notify-customer", version: "1.0.0" },
    { id: "v-2", name: "notify-customer", version: "2.0.0" },
    { id: "v-9", name: "audit", version: "1.0.0" },
  ];
  const site = (reference: string, path = "/spec/steps/0/uses") => ({
    reference,
    path,
  });
  const NOTIFY = [site("notify-customer@1.0.0")];

  it("picks the latest activation of the callee's name when it runs the called version", () => {
    const activations: CalleeActivation[] = [
      {
        id: "act-1",
        name: "notify-customer",
        revision: 1,
        retired: false,
        versionId: "v-1",
      },
      {
        id: "act-2",
        name: "notify-customer",
        revision: 2,
        retired: false,
        versionId: "v-1",
        createdAt: "2026-11-12T12:00:00Z",
      },
      {
        id: "other",
        name: "audit",
        revision: 7,
        retired: false,
        versionId: "v-9",
      },
    ];
    const rows = proposePins(NOTIFY, versions, activations);
    expect(rows).toEqual([
      {
        kind: "proposed",
        reference: "notify-customer@1.0.0",
        activationId: "act-2",
        text: "notify-customer 1.0.0 · activation of 12 Nov",
      },
    ]);
    expect(callPins(rows)).toEqual({ "notify-customer@1.0.0": "act-2" });
  });

  it("names the activation by its revision when the platform doesn't say when it was made", () => {
    const rows = proposePins(NOTIFY, versions, [
      {
        id: "act-3",
        name: "notify-customer",
        revision: 3,
        retired: false,
        versionId: "v-1",
      },
    ]);
    expect(rows[0].text).toBe("notify-customer 1.0.0 · activation 3");
  });

  it.each([
    ["no activation", []],
    [
      "a latest activation that runs another version",
      [
        {
          id: "act-1",
          name: "notify-customer",
          revision: 1,
          retired: false,
          versionId: "v-1",
        },
        {
          id: "act-2",
          name: "notify-customer",
          revision: 2,
          retired: false,
          versionId: "v-2",
        },
      ],
    ],
    [
      "a retired latest activation",
      [
        {
          id: "act-1",
          name: "notify-customer",
          revision: 4,
          retired: true,
          versionId: "v-1",
        },
      ],
    ],
  ] as [string, CalleeActivation[]][])(
    "asks to activate the callee first with %s",
    (_, activations) => {
      const rows = proposePins(NOTIFY, versions, activations);
      expect(rows).toEqual([
        {
          kind: "missing",
          reference: "notify-customer@1.0.0",
          text: "Activate notify-customer first",
        },
      ]);
      expect(callPins(rows)).toEqual({});
    },
  );

  it("asks for an unpublished called version too", () => {
    const rows = proposePins([site("notify-customer@3.0.0")], versions, [
      {
        id: "act-1",
        name: "notify-customer",
        revision: 1,
        retired: false,
        versionId: "v-1",
      },
    ]);
    expect(rows[0].kind).toBe("missing");
  });

  it("lists each called version once, in the order the definition calls them", () => {
    const rows = proposePins(
      [
        site("notify-customer@1.0.0"),
        site("audit@1.0.0", "/spec/steps/1/uses"),
        site("notify-customer@1.0.0", "/spec/steps/2/uses"),
      ],
      versions,
      [],
    );
    expect(rows.map((row) => row.reference)).toEqual([
      "notify-customer@1.0.0",
      "audit@1.0.0",
    ]);
  });

  it("shows the compiler's message for a callee that doesn't allow this caller", () => {
    const message =
      "This workflow version does not allow calls from orders; add orders to its callable.allowedCallers in a new version.";
    const rows = proposePins(
      [
        site("audit@1.0.0", "/spec/steps/1/uses"),
        site("notify-customer@1.0.0", "/spec/steps/0/tools/0/workflow"),
      ],
      versions,
      [
        {
          id: "a",
          name: "audit",
          revision: 1,
          retired: false,
          versionId: "v-9",
        },
      ],
      [
        {
          code: "WV-COMP-CALL_NOT_CALLABLE",
          message,
          path: "/spec/steps/0/tools/0/workflow",
        },
        {
          code: "WV-COMP-CALL_DEPTH",
          message: "Too deep",
          path: "/spec/steps/1/uses",
        },
        {
          code: "WV-COMP-CALL_NOT_CALLABLE",
          message: "elsewhere",
          path: "/spec/steps/9/uses",
        },
      ],
    );
    expect(rows.map((row) => [row.kind, row.text])).toEqual([
      ["proposed", "audit 1.0.0 · activation 1"],
      ["not-callable", message],
    ]);
  });
});
