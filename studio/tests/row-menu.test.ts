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
// A menu item that can't run now: a disabled one does nothing, while a
// blocked one (a command with a reason, as its toolbar button) is marked
// disabled and still runs, so it can say why.
import { describe, expect, it } from "vitest";
import {
  markedDisabled,
  runsWhenPicked,
  type RowMenuItem,
} from "../src/app/row-menu";

const item = (extra: Partial<RowMenuItem> = {}): RowMenuItem => ({
  label: "Publish…",
  run: () => undefined,
  ...extra,
});

describe("row menu items", () => {
  it("marks a blocked item disabled and still runs it", () => {
    const blocked = item({
      blocked: true,
      detail: "Your account can't publish workflows in this workspace.",
    });
    expect([markedDisabled(blocked), runsWhenPicked(blocked)]).toEqual([
      true,
      true,
    ]);
  });

  it("never runs a disabled item", () => {
    const disabled = item({ disabled: true });
    expect([markedDisabled(disabled), runsWhenPicked(disabled)]).toEqual([
      true,
      false,
    ]);
    expect([markedDisabled(item()), runsWhenPicked(item())]).toEqual([
      false,
      true,
    ]);
  });
});
