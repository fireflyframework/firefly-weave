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
import { buildCatalog } from "../src/app/editor/add-step/entries";
import { searchPanel } from "../src/app/editor/add-step/search";
import { catalogInput, installed, published } from "./add-step-fixtures";

const catalog = buildCatalog(
  catalogInput({ connectors: installed, actions: published, catalog: "ready" }),
);
const search = (query: string, triggersFirst = false) =>
  searchPanel(
    query,
    null,
    catalog.searchable,
    triggersFirst,
    catalog.customCall,
  );
const labels = (query: string, triggersFirst = false) =>
  search(query, triggersFirst).sections.flatMap((section) =>
    section.entries.map((entry) => entry.label),
  );
const headings = (query: string, triggersFirst = false) =>
  search(query, triggersFirst).sections.map((section) => section.heading);

describe("searching Add a step", () => {
  it.each(["http", "api", "request", "url"])(
    "finds Call an API first for %s",
    (word) => {
      expect(labels(word)[0]).toBe("Call an API");
    },
  );

  it("lists the Email trigger before Send email and Reply to an email, under Triggers then Actions, while there is no trigger", () => {
    expect(headings("mail", true)).toEqual(["Triggers", "Actions"]);
    const found = labels("mail", true);
    expect(found[0]).toBe("Email");
    expect(found.indexOf("Send email")).toBeGreaterThan(0);
    expect(found.indexOf("Reply to an email")).toBeGreaterThan(
      found.indexOf("Send email"),
    );
  });

  it("puts steps first, then actions, then triggers once the workflow has a trigger", () => {
    expect(headings("message")).toEqual(["Steps", "Actions", "Triggers"]);
  });

  it("finds a person's steps by the everyday word", () => {
    expect(labels("approval")[0]).toBe("Approve or reject");
  });

  it("finds older versions only by their version", () => {
    expect(labels("sql")).toEqual(["sql.lookup", "PostgreSQL"]);
    expect(
      search("sql.lookup 1.0.0").sections.flatMap((section) =>
        section.entries.map((entry) => entry.description),
      ),
    ).toEqual(["Version 1.0.0 · read"]);
  });

  it("needs every word to match, and offers a custom API call when nothing does", () => {
    const outcome = search("send loop");
    expect(outcome.note).toBe("Nothing matches “send loop”.");
    expect(outcome.sections).toEqual([
      { heading: "Can't find it?", entries: [catalog.customCall] },
    ]);
  });

  it("looks in the open view first, then elsewhere", () => {
    const flow = catalog.views
      .get("category:flow")!
      .sections.flatMap((section) => section.entries);
    expect(
      searchPanel(
        "loop",
        flow,
        catalog.searchable,
        false,
        catalog.customCall,
      ).sections[0].entries.map((entry) => entry.label),
    ).toEqual(["Loop over items"]);
    const elsewhere = searchPanel(
      "email",
      flow,
      catalog.searchable,
      false,
      catalog.customCall,
    );
    expect(elsewhere.sections.map((section) => section.heading)).toEqual([
      "Results in other categories",
      "Can't find it?",
    ]);
    expect(elsewhere.sections[0].entries[0].label).toBe("Email");
  });
});
