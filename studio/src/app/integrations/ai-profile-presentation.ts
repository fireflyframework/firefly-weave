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
import { getAt } from "../forms/core/json";
import { resolveSchema } from "../forms/core/resolve";
import { isRecord, withValue, type Data, type Schema } from "../task-schema";

export interface ProfileSection {
  schema: Schema;
  paths: string[][];
}

/** Presentation copies retain the host's constraints and local reference definitions. */
export function aiProfileSections(schema: Schema) {
  const root = resolveSchema(schema).schema;
  const properties = isRecord(root["properties"]) ? root["properties"] : {};
  const prefix = properties["profile"] ? ["profile"] : [];
  const basics = [["provider"], ["model"], ["options", "max_tokens"]].map(
    (path) => [...prefix, ...path].join("/"),
  );
  const section = (basic: boolean): ProfileSection => {
    const paths: string[][] = [];
    const project = (source: unknown, path: string[]): Schema | null => {
      const resolved = resolveSchema(source, { root: schema });
      const node = resolved.schema;
      const children = isRecord(node["properties"]) ? node["properties"] : {};
      if (Object.keys(children).length && !resolved.cyclic) {
        const selected: Record<string, Schema> = {};
        for (const [key, value] of Object.entries(children)) {
          const child = project(value, [...path, key]);
          if (child) selected[key] = child;
        }
        if (!Object.keys(selected).length) return null;
        const key = path.at(-1);
        const title =
          key === "profile"
            ? "Model settings"
            : key === "options"
              ? basic
                ? "Response length"
                : "Generation options"
              : key === "reasoning"
                ? "Reasoning strategy"
                : node["title"];
        return {
          ...node,
          ...(typeof title === "string" ? { title } : {}),
          properties: selected,
          required: Array.isArray(node["required"])
            ? node["required"].filter(
                (name): name is string =>
                  typeof name === "string" && name in selected,
              )
            : [],
        } as Schema;
      }
      if (!path.length || basics.includes(path.join("/")) !== basic)
        return null;
      paths.push(path);
      return { ...node } as Schema;
    };
    return {
      schema: { ...project(schema, []), $defs: root["$defs"] } as Schema,
      paths,
    };
  };
  return { basic: section(true), advanced: section(false) };
}

export function profileSectionData(data: Data, section: ProfileSection): Data {
  let selected: Data = {};
  for (const path of section.paths)
    selected = withValue(selected, path, getAt(data, path));
  return selected;
}

/** Replace owned leaves, including removals; other sections and unknown data survive. */
export function mergeProfileSection(
  data: Data,
  change: Data,
  section: ProfileSection,
): Data {
  let merged = structuredClone(data);
  for (const path of section.paths)
    merged = withValue(merged, path, getAt(change, path));
  return merged;
}
