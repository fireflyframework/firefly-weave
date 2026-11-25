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
// The problems a check finds in the Platform event trigger, or in the input it
// needs, as the trigger says them, and the part of its Parameters that fixes
// them. The compiler knows only that a value or a type doesn't fit there.
import type { EventPart } from "../../designer/diagnostic-location";
import { isJsonObject } from "../../forms/core/json";
import type { Workflow } from "../../model";
import {
  eventProblems,
  INPUT_PATH,
  INPUT_REFUSED,
  INPUT_UNSURE,
  readDeclaration,
} from "./event-trigger";

export interface EventProblem {
  part: EventPart;
  /** What is wrong, as the trigger says it; null when it has no sentence for it. */
  text: string | null;
  hint?: string;
}
/** Each part as its heading names it in Parameters. */
const PARTS: Readonly<Record<EventPart, string>> = {
  events: "Which events",
  from: "From which workflows",
  input: "Input",
};
/** "Platform event trigger · Which events". */
export const eventPlace = (part: EventPart) =>
  `Platform event trigger · ${PARTS[part]}`;

/** What a check found at the trigger `definition` declares; null for anything else. */
export function eventProblem(
  { code, path }: { code?: unknown; path?: unknown },
  definition: unknown,
): EventProblem | null {
  if (
    typeof path !== "string" ||
    !isJsonObject(definition) ||
    !isJsonObject(definition["spec"])
  )
    return null;
  const declaration = readDeclaration(definition as unknown as Workflow);
  if (!declaration) return null;
  if (path === INPUT_PATH) {
    const found = (name: string) =>
      typeof code === "string" && code.endsWith(name);
    if (found("TYPE_MISMATCH"))
      return {
        part: "input",
        text: INPUT_REFUSED,
        hint: "Reset the input in the trigger's Parameters.",
      };
    return found("UNKNOWN_COMPATIBILITY")
      ? { part: "input", text: INPUT_UNSURE, hint: "Each start checks it." }
      : null;
  }
  const [, spec, field, part = "events"] = path.split("/");
  if (spec !== "spec" || field !== "eventTrigger") return null;
  if (part !== "events" && part !== "from") return null;
  return { part, text: eventProblems(declaration)[part] ?? null };
}
