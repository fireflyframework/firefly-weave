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
// Human task: who answers, the question, the answers; what the person sees
// and fills in are options; deadlines live on Settings.
import type { FormSpec } from "../../registry";
import { durationParam } from "./shared";

export const humanForm = (): FormSpec => ({
  fields: [
    {
      id: "assignment",
      path: ["assignment"],
      type: "text",
      label: "Assign to",
      required: true,
      identifier: true,
      default: "reviewers",
      hint: "The group of people who can answer. You choose who is in it when you activate.",
    },
    {
      id: "title",
      path: ["title"],
      type: "text",
      label: "Title",
      required: true,
      mapping: "both",
      templateCapable: true,
    },
    {
      id: "answers",
      path: ["decisions"],
      type: "list",
      label: "Answers",
      required: true,
      display: "chips",
      default: ["approve", "reject"],
      minItems: 1,
      maxItems: 32,
      addLabel: "Add answer",
      hint: "Use unique names. Renaming an answer updates its paths.",
      item: {
        id: "answer",
        path: [],
        type: "text",
        label: "Answer",
        identifier: true,
      },
    },
  ],
  options: [
    {
      id: "context",
      path: ["context"],
      type: "keyValue",
      label: "Details to show",
      mapping: "both",
      default: {},
      addLabel: "Add detail",
      hint: "What the person sees next to the question.",
    },
    {
      id: "form",
      path: ["formSchema"],
      type: "schema",
      label: "Form fields",
      default: { type: "object", properties: {} },
      hint: "Fields the person fills in with the answer.",
    },
  ],
});

export const humanSettings = (): FormSpec => ({
  fields: [
    durationParam("due", ["dueSeconds"], "Due after", {
      hint: "When the task shows as overdue.",
    }),
    durationParam("expiry", ["expirySeconds"], "Expires after", {
      hint: "When the task closes without an answer.",
    }),
  ],
});
