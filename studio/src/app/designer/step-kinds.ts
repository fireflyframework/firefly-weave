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
// One set of names for step kinds: the palette, the step picker, canvas
// nodes, templates and data suggestions all read them from here.
import type { Kind } from "../model";

/** Plain-language names for step kinds. */
export const stepKindLabels: Record<Kind, string> = {
  action: "Call an action",
  decisionTable: "Decision table",
  llm: "AI task",
  transform: "Transform",
  switch: "Decision",
  parallel: "Parallel",
  wait: "Wait for time",
  signal: "Wait for signal",
  humanTask: "Human task",
  fail: "Fail",
};
export const stepKindDescriptions: Record<Kind, string> = {
  action: "Run a published action",
  decisionTable: "Evaluate reusable rules",
  llm: "Ask an AI model for a typed result",
  transform: "Shape and map data",
  switch: "Follow a different path based on a condition",
  parallel: "Run independent branches at the same time",
  wait: "Continue after a duration",
  signal: "Wait for a message from another system",
  humanTask: "Ask a person to decide",
  fail: "Stop the run with a clear reason",
};

/** Everyday words people search with, so "approval" finds the human task. */
export const stepKindKeywords: Record<Kind, string> = {
  action: "api http request rest webhook connector worker integration",
  decisionTable: "rules policy evaluate table first unique collect",
  llm: "ai model llm prompt reasoning generate agentic",
  transform: "map set assign merge format",
  switch: "if else condition branch route rule decision",
  parallel: "fork concurrent split",
  wait: "delay sleep timer pause",
  signal: "event message callback receive",
  humanTask: "approval approve review person form manual",
  fail: "error throw stop abort",
};
/**
 * The palette's groups, in order. "Actions" also holds the published
 * actions the palette lists under it.
 */
export const stepKindGroups: { label: string; kinds: Kind[] }[] = [
  { label: "Logic", kinds: ["switch", "decisionTable", "parallel", "fail"] },
  { label: "Data", kinds: ["transform"] },
  { label: "Waiting", kinds: ["wait", "signal"] },
  { label: "People", kinds: ["humanTask"] },
  { label: "Actions", kinds: ["action", "llm"] },
];
