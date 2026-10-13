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
// Searching the Add a step panel. Every word must match an entry's name,
// everyday words or description: names count most, then the words people
// use ("http" finds Call an API first), then descriptions, from the start
// of their words. Results group as
// Triggers, Steps and Actions; with nothing in the open view, results
// elsewhere come next, then "Make a custom API call".
import type { EntrySection, PanelEntry, PanelSection } from "./entries";

export interface SearchOutcome {
  sections: PanelSection[];
  /** One sentence when nothing matches anywhere. */
  note: string;
}

const SEMVER = /\d+\.\d+\.\d+/;
const HEADINGS: Readonly<Record<EntrySection, string>> = {
  triggers: "Triggers",
  steps: "Steps",
  actions: "Actions",
};
const words = (text: string) => text.toLowerCase().split(/\s+/).filter(Boolean);

/** How well one word matches an entry: lower is better; null when it doesn't. */
function wordScore(word: string, entry: PanelEntry): number | null {
  if (words(entry.aliases).includes(word)) return 0;
  const label = entry.label.toLowerCase();
  if (label.startsWith(word)) return 1;
  if (label.split(/[^a-z0-9]+/).some((part) => part.startsWith(word))) return 2;
  if (label.includes(word)) return 3;
  if (words(entry.keywords).some((keyword) => keyword.startsWith(word)))
    return 4;
  if (
    entry.keywords
      .toLowerCase()
      .split(/[^a-z0-9]+/)
      .some((part) => part.startsWith(word))
  )
    return 5;
  // Everyday words and descriptions match from the start of their words (or
  // of their parts, "lookup" in "sql.lookup"): a short word ("if") never
  // matches inside another ("notify").
  const description = entry.description.toLowerCase();
  if (
    [...words(description), ...description.split(/[^a-z0-9]+/)].some((part) =>
      part.startsWith(word),
    )
  )
    return 6;
  return null;
}

/** The sum of the query's word scores, or null when a word doesn't match. */
export function matchScore(query: string, entry: PanelEntry): number | null {
  let total = 0;
  for (const word of words(query)) {
    const score = wordScore(word, entry);
    if (score === null) return null;
    total += score;
  }
  return total;
}

function matches(query: string, pool: readonly PanelEntry[]): PanelEntry[] {
  const versioned = SEMVER.test(query);
  return pool
    .map((entry, index) => ({
      entry,
      index,
      score: entry.older && !versioned ? null : matchScore(query, entry),
    }))
    .filter(
      (hit): hit is { entry: PanelEntry; index: number; score: number } =>
        hit.score !== null,
    )
    .sort((a, b) => a.score - b.score || a.index - b.index)
    .map((hit) => hit.entry);
}

/** Triggers first while the workflow has no trigger; otherwise Steps, Actions, Triggers. */
export function grouped(
  hits: readonly PanelEntry[],
  triggersFirst: boolean,
): PanelSection[] {
  const order: EntrySection[] = triggersFirst
    ? ["triggers", "steps", "actions"]
    : ["steps", "actions", "triggers"];
  return order
    .map((section) => ({
      heading: HEADINGS[section],
      entries: hits.filter((hit) => hit.section === section),
    }))
    .filter((section) => section.entries.length);
}

/** `scope` is the open category or app; null searches everything. */
export function searchPanel(
  query: string,
  scope: readonly PanelEntry[] | null,
  everything: readonly PanelEntry[],
  triggersFirst: boolean,
  customCall: PanelEntry,
): SearchOutcome {
  const here = matches(query, scope ?? everything);
  if (here.length) return { sections: grouped(here, triggersFirst), note: "" };
  const fallback: PanelSection = {
    heading: "Can't find it?",
    entries: [customCall],
  };
  const elsewhere = scope ? matches(query, everything) : [];
  if (elsewhere.length)
    return {
      sections: [
        { heading: "Results in other categories", entries: elsewhere },
        fallback,
      ],
      note: "",
    };
  return { sections: [fallback], note: `Nothing matches “${query.trim()}”.` };
}
