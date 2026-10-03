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
import { describe, expect, it } from "vitest";
import { FieldsDraft } from "../src/app/forms/core/fields";

describe("Fields builder draft", () => {
  it("keeps a literal object intact until a named field is edited", () => {
    const source = { literal: { amount: 10, nested: { keep: true } } };
    const draft = new FieldsDraft(source);
    expect(draft.expression()).toBe(source);
    draft.add();
    expect(draft.expression()).toBe(source);
    expect(draft.valid).toBe(true);
    draft.update(draft.rows[0], { ref: "/input/amount" });
    expect(draft.expression()).toEqual({
      object: {
        amount: { ref: "/input/amount" },
        nested: { literal: { keep: true } },
      },
    });
  });
  it("a named row accepts data and preserves row order on rename and move", () => {
    const draft = new FieldsDraft({ literal: {} });
    const first = draft.add();
    draft.rename(first, "total");
    draft.update(first, { ref: "/input/amount" });
    expect(draft.expression()).toEqual({
      object: { total: { ref: "/input/amount" } },
    });
    const second = draft.add();
    draft.rename(second, "note");
    draft.update(second, { literal: "Keep" });
    draft.move(second, -1);
    expect(
      Object.keys((draft.expression() as { object: object }).object),
    ).toEqual(["note", "total"]);
    draft.rename(second, "__proto__");
    expect(
      Object.hasOwn(
        (draft.expression() as { object: object }).object,
        "__proto__",
      ),
    ).toBe(true);
    draft.remove(first);
    expect(draft.expression()).toEqual({ literal: { ["__proto__"]: "Keep" } });
  });
  it("duplicate names remain invalid while a sibling changes and recover on rename", () => {
    const draft = new FieldsDraft({
      object: { a: { literal: 1 }, b: { literal: 2 } },
    });
    draft.rename(draft.rows[0], "b");
    expect(draft.valid).toBe(false);
    draft.update(draft.rows[1], { literal: 3 });
    expect(draft.valid).toBe(false);
    draft.rename(draft.rows[0], "c");
    expect(draft.valid).toBe(true);
    expect(draft.expression()).toEqual({ literal: { c: 1, b: 3 } });
  });
});
