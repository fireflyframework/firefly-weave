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
import { describe, expect, it, vi } from "vitest";
import { moveToStep } from "../src/app/editor/ndv/step-move";

function host(opens: boolean, error = "") {
  return {
    error,
    openStepDetails: vi.fn(async () => opens),
    notify: vi.fn(),
  };
}

describe("moving step details to another step", () => {
  it("opens the step through the editor's opener with the request", async () => {
    const editor = host(true);
    expect(
      await moveToStep(editor, "lookup", { tab: "settings" }, () => true),
    ).toBe(true);
    expect(editor.openStepDetails).toHaveBeenCalledWith("lookup", {
      tab: "settings",
    });
    expect(editor.notify).not.toHaveBeenCalled();
  });

  it("shows why a refused move didn't happen, above step details", async () => {
    const reason =
      "Fix the invalid fields before continuing. Your edits are still here.";
    const editor = host(false, reason);
    expect(await moveToStep(editor, "lookup", {}, () => true)).toBe(false);
    expect(editor.notify).toHaveBeenCalledWith(reason, undefined, "danger");
  });

  it("names the step when the editor gives no reason", async () => {
    const editor = host(false);
    await moveToStep(editor, "route-by-value", {}, () => true);
    expect(editor.notify).toHaveBeenCalledWith(
      "Couldn't open route-by-value.",
      undefined,
      "danger",
    );
  });

  it("stays quiet when step details closed in the meantime", async () => {
    const editor = host(false, "Anything");
    await moveToStep(editor, "lookup", {}, () => false);
    expect(editor.notify).not.toHaveBeenCalled();
  });
});
