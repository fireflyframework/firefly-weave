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
// Which fields a form shows now: the default fields in order, then the
// options that are added (stored value differs from the default, added in
// this session, or required while visible), in spec order. The rest are Add
// option entries; hidden or gated ones stay listed, disabled with a reason.
import type { Step } from "../../../model";
import type { FormSpec, KindContext, ParamSpec } from "../registry";
import {
  applyChange,
  isDefault,
  resetChanges,
  type FormChange,
  type FormSubject,
} from "./value-io";

export interface FormEnv {
  subject: FormSubject;
  step: Step;
  kind: KindContext;
  /** Why the whole dialog is read-only (source error, locked editing), or null. */
  readOnly: string | null;
  /** Options the person added in this session. */
  added: ReadonlySet<string>;
}
export interface FieldEntry {
  spec: ParamSpec;
  option: boolean;
  readOnly: string | null;
  disabled: string | null;
}
export interface OptionEntry {
  spec: ParamSpec;
  disabled: string | null;
}
export interface FormState {
  fields: FieldEntry[];
  addable: OptionEntry[];
}

export const featureReason = (feature: string): string =>
  `Update the platform to use this (${feature}).`;

/** A group without a value of its own (`path: []`) is at its default when all its children are. */
export function paramIsDefault(spec: ParamSpec, env: FormEnv): boolean {
  if (spec.type === "fields" && spec.children && spec.path.length === 0)
    return spec
      .children(env.step, env.kind)
      .every((child) => paramIsDefault(child, env));
  return isDefault(env.subject, spec);
}

/** The writes that put a field (or each child of a value-less group) back to its default. */
export function resetParam(spec: ParamSpec, env: FormEnv): FormChange[] {
  if (!(spec.type === "fields" && spec.children && spec.path.length === 0))
    return resetChanges(env.subject, spec);
  const changes: FormChange[] = [];
  let subject = env.subject;
  for (const child of spec.children(env.step, env.kind))
    for (const change of resetParam(child, { ...env, subject })) {
      changes.push(change);
      subject = applyChange(subject, change);
    }
  return changes;
}

export function formState(form: FormSpec, env: FormEnv): FormState {
  const visible = (spec: ParamSpec) =>
    !spec.showWhen || spec.showWhen(env.step, env.kind);
  const disabled = (spec: ParamSpec) =>
    spec.feature && !env.kind.features.includes(spec.feature)
      ? featureReason(spec.feature)
      : null;
  const readOnly = (spec: ParamSpec) =>
    env.readOnly ?? spec.readOnly?.(env.step, env.kind) ?? null;
  const entry = (spec: ParamSpec, option: boolean): FieldEntry => ({
    spec,
    option,
    readOnly: readOnly(spec),
    disabled: disabled(spec),
  });
  const fields = form.fields.filter(visible).map((spec) => entry(spec, false));
  const addable: OptionEntry[] = [];
  for (const spec of form.options ?? []) {
    const shown = visible(spec);
    if (
      shown &&
      (spec.required || env.added.has(spec.id) || !paramIsDefault(spec, env))
    )
      fields.push(entry(spec, true));
    else
      addable.push({
        spec,
        disabled: shown
          ? disabled(spec)
          : (spec.hiddenReason?.(env.step, env.kind) ??
            "Not available for this step now."),
      });
  }
  return { fields, addable };
}

/**
 * The writes that clear fields a change just hid (clear-on-hide), with
 * their labels and reasons for the Undo toast. Fields that keep their
 * value, fields at their default and options removed on purpose are left.
 */
export function hiddenChanges(
  before: FormState,
  after: FormState,
  env: FormEnv,
  removed: ReadonlySet<string>,
): { changes: FormChange[]; labels: string[]; reasons: string[] } {
  const still = new Set(after.fields.map((field) => field.spec.id));
  const reasons = new Map(
    after.addable.map((option) => [option.spec.id, option.disabled]),
  );
  const changes: FormChange[] = [];
  const labels: string[] = [];
  const why: string[] = [];
  let subject = env.subject;
  for (const { spec } of before.fields) {
    if (
      still.has(spec.id) ||
      removed.has(spec.id) ||
      spec.whenHidden === "keep"
    )
      continue;
    const at = { ...env, subject };
    if (paramIsDefault(spec, at)) continue;
    for (const change of resetParam(spec, at)) {
      changes.push(change);
      subject = applyChange(subject, change);
    }
    labels.push(spec.label);
    const reason = reasons.get(spec.id);
    if (reason) why.push(reason);
  }
  return { changes, labels, reasons: why };
}
