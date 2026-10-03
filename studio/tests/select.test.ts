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
import { describe, it, expect } from "vitest";
import { selectOptions } from "../src/app/forms/ui/select";

describe("themed choice options", () => {
  it("filters every word over the label, group and explanation", () => {
    const options = [
      {
        value: "one",
        label: "Customer number",
        description: "The external account",
        group: "Inputs",
      },
      {
        value: "two",
        label: "Invoice number",
        description: "The issued invoice",
        group: "Results",
      },
    ];
    expect(selectOptions(options, "input account")).toEqual([options[0]]);
    expect(selectOptions(options, "invoice missing")).toEqual([]);
  });
  it("keeps option order and first identity including the optional empty choice", () => {
    const options = [
      { value: "", label: "Not set" },
      { value: "a", label: "A" },
      { value: "a", label: "Duplicate" },
      { value: "b", label: "B" },
    ];
    expect(selectOptions(options, "")).toEqual([
      options[0],
      options[1],
      options[3],
    ]);
  });
});
