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
// One field: the label row (label, required star, help, Fixed | Mapped and
// the field menu), the control, and one line under it (the message, else the
// resolved preview, else the hint or the read-only reason). This component
// draws the simple controls itself; lists, key-value rows, groups,
// conditions, resources, connections, schemas and URLs have components of
// their own.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  inject,
  input,
  viewChild,
} from "@angular/core";
import {
  durationSeconds,
  splitDuration,
} from "../../../designer/canvas-summary";
import { canonicalJson } from "../../../forms/core/json";
import { localDateTime, storedDateTime } from "../../../forms/core/form-model";
import { Select, type SelectOption } from "../../../forms/ui/select";
import { Icon } from "../../../icon";
import { RowMenu, type RowMenuItem } from "../../../row-menu";
import type { Choice, Json, ParamSpec } from "../registry";
import type { FieldEntry } from "./form-model";
import { ListField } from "./list-field";
import { KeyValueField } from "./key-value-field";
import { FieldsField } from "./fields-field";
import { FormulaField } from "./formula-field";
import type { FormSession } from "./form-session";
import { normalizeIdentifier } from "./identifiers";
import { Toggletip } from "./toggletip";
import { ABSENT } from "./value-io";

let sequence = 0;
const UNIT_SECONDS: Record<string, number> = {
  seconds: 1,
  minutes: 60,
  hours: 3600,
  days: 86400,
};
const UNIT_CODE: Record<string, string> = {
  seconds: "s",
  minutes: "min",
  hours: "h",
  days: "d",
};

@Component({
  selector: "weave-param-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    Icon,
    RowMenu,
    Select,
    Toggletip,
    FormulaField,
    ListField,
    KeyValueField,
    FieldsField,
  ],
  templateUrl: "./param-field.html",
  host: {
    class: "param-host",
    "[class.is-option]": "entry().option && isGroup()",
    "[attr.data-param]": "spec().id",
    "[attr.data-mode]": "mode()",
    "[attr.data-required-empty]": "session().requiredEmpty(spec()) ? '' : null",
  },
})
export class ParamField {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  entry = input.required<FieldEntry>();
  row = input(false);
  isGroup(): boolean {
    return ["list", "keyValue", "fields", "conditions", "schema"].includes(
      this.spec().type,
    );
  }
  readonly String = String;
  private readonly formula = viewChild(FormulaField);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  readonly uid = `p${++sequence}`;
  choices: Choice[] = [];
  private readonly lifetime = inject(DestroyRef);
  private loadedChoices: {
    session: FormSession;
    descriptor: string;
    provider: ParamSpec["choices"];
    profile: FormSession["host"]["profile"];
    api: FormSession["host"]["api"];
  } | null = null;
  private choicesLoading = false;
  private choicesError = "";
  numberText: string | null = null;

  ids() {
    const id = this.spec().id.replace(/[^A-Za-z0-9_-]/g, "-");
    return {
      control: `${this.uid}-${id}`,
      line: `${this.uid}-${id}-line`,
      help: `${this.uid}-${id}-help`,
    };
  }
  describedBy(): string {
    const ids = this.ids();
    return [ids.line, this.spec().description ? ids.help : ""]
      .filter(Boolean)
      .join(" ");
  }
  value() {
    return this.session().read(this.spec());
  }
  fixedValue(): Json | undefined {
    const value = this.value();
    return value.mode === "fixed" ? value.value : undefined;
  }
  mode() {
    return this.session().mode(this.spec());
  }
  readOnly(): string | null {
    return this.session().readOnly(this.entry());
  }
  line() {
    const line = this.session().line(this.spec(), this.entry());
    if (line.kind === "error" || line.kind === "warning") return line;
    if (this.choicesError)
      return {
        kind: "error" as const,
        text: this.choicesError,
        fixLabel: "Retry",
        fix: () => {
          this.loadedChoices = null;
          this.choicesError = "";
          this.loadChoices();
          this.session().host.refreshView();
        },
      };
    if (this.choicesLoading)
      return { kind: "hint" as const, text: "Loading choices…" };
    return line;
  }
  problemShown(): boolean {
    const kind = this.line().kind;
    return kind === "error" || kind === "warning";
  }

  // ------------------------------------------------------------- menu
  menuItems(): RowMenuItem[] {
    const spec = this.spec();
    const session = this.session();
    const locked = !!this.readOnly();
    const items: RowMenuItem[] = [
      {
        label: "Reset to default",
        disabled: locked || session.atDefault(spec),
        run: () => session.clear(spec),
      },
    ];
    if (this.mode() !== null)
      items.push({
        label: "Use data…",
        disabled: locked,
        run: () => this.setMode("mapped"),
      });
    if (this.mode() === "mapped")
      items.push({
        label: "Edit formula",
        detail: "Formulas are edited with Edit as YAML in the More menu.",
        disabled: true,
        run: () => undefined,
      });
    if (
      ["list", "keyValue", "fields"].includes(spec.type) &&
      spec.mapping !== "fixed" &&
      this.session()
        .subject()
        .roots.some((root) => String(root[0]) === String(spec.path[0]))
    )
      items.push(
        this.session().wholeMapping(spec)
          ? {
              label: "Use rows",
              disabled: locked,
              run: () => void session.useRows(spec),
            }
          : {
              label:
                spec.type === "list"
                  ? "Map the whole list"
                  : "Map the whole input",
              disabled: locked,
              run: () => this.setMode("mapped"),
            },
      );
    items.push({ label: "Copy value", run: () => void this.copyValue() });
    if (this.entry().option && !spec.required)
      items.push({
        label: "Remove option",
        danger: true,
        disabled: locked,
        run: () => session.removeOption(spec),
      });
    return items;
  }
  private async copyValue() {
    const value = this.value();
    const text =
      value.mode === "absent"
        ? ""
        : JSON.stringify(
            value.mode === "fixed" ? value.value : value.expression,
          );
    try {
      await navigator.clipboard.writeText(text);
      this.session().announce(`Copied the value of ${this.spec().label}.`);
    } catch {
      this.session().announce("Studio couldn't reach the clipboard.");
    }
  }
  private focusControl(
    session = this.session(),
    spec = this.spec(),
    pick = false,
  ) {
    setTimeout(() => {
      if (
        !this.lifetime.destroyed &&
        this.session() === session &&
        canonicalJson(this.spec()) === canonicalJson(spec) &&
        session.isCurrent()
      ) {
        if (pick) {
          if (this.readOnly()) return;
          this.formula()?.beginPick();
          this.focusControl(session, spec);
          return;
        }
        this.element.nativeElement
          .querySelector<HTMLElement>(
            ".param-control :is(input, textarea, select, button, [tabindex='0'])",
          )
          ?.focus();
      }
    });
  }

  // ------------------------------------------------------------ modes
  setMode(mode: "fixed" | "mapped") {
    const session = this.session();
    const spec = this.spec();
    void session.setMode(spec, mode).then(() => {
      if (
        this.lifetime.destroyed ||
        this.session() !== session ||
        canonicalJson(this.spec()) !== canonicalJson(spec) ||
        !session.isCurrent() ||
        this.readOnly()
      )
        return;
      this.focusControl(session, spec, mode === "mapped");
    });
  }
  toolsKey(event: KeyboardEvent) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const items = [
      ...this.element.nativeElement.querySelectorAll<HTMLElement>(
        ".param-tools [role=radio], .param-tools weave-row-menu > button",
      ),
    ];
    const at = items.indexOf(document.activeElement as HTMLElement);
    if (at < 0) return;
    event.preventDefault();
    items[
      (at + (event.key === "ArrowRight" ? 1 : items.length - 1)) % items.length
    ].focus();
  }
  /** "=" in an empty Fixed field switches it to Mapped. */
  controlKey(event: KeyboardEvent) {
    if (event.key !== "=" || this.mode() !== "fixed") return;
    const target = event.target as HTMLInputElement;
    if (target.value) return;
    event.preventDefault();
    event.stopPropagation();
    this.setMode("mapped");
  }

  // ---------------------------------------------------------- writing
  left() {
    this.session().touch(this.spec());
    this.numberText = null;
  }
  text(event: Event) {
    const value = (event.target as HTMLInputElement).value;
    const spec = this.spec();
    if (value === "" && !spec.required)
      return this.session().write(spec, ABSENT);
    this.session().write(spec, { mode: "fixed", value });
  }
  /** Identifiers are typed freely and stored normalized when the field is left. */
  identifierLeft(event: Event) {
    const input = event.target as HTMLInputElement;
    const normalized = normalizeIdentifier(input.value);
    if (normalized && normalized !== input.value) {
      input.value = normalized;
      this.session().write(
        this.spec(),
        { mode: "fixed", value: normalized },
        `${this.spec().id}:normalize`,
      );
    }
    this.left();
  }
  number(event: Event) {
    const text = (event.target as HTMLInputElement).value.trim();
    this.numberText = text;
    if (!text) return this.session().clear(this.spec());
    const value = Number(text);
    if (Number.isFinite(value)) {
      this.numberText = null;
      this.session().write(this.spec(), { mode: "fixed", value });
    }
  }
  numberProblem(): string {
    return this.numberText !== null &&
      this.numberText !== "" &&
      !Number.isFinite(Number(this.numberText))
      ? "Enter a number."
      : "";
  }
  units(): string[] {
    return [...(this.spec().units ?? ["seconds", "minutes", "hours", "days"])];
  }
  durationParts(): { amount: string; unit: string } {
    const seconds = this.fixedValue();
    const split = splitDuration(seconds);
    const unit =
      Object.keys(UNIT_CODE).find((u) => UNIT_CODE[u] === split.unit) ??
      "minutes";
    const usable = this.units().includes(unit) ? unit : this.units()[0];
    if (typeof seconds !== "number") return { amount: "", unit: usable };
    return {
      amount: String(
        Math.round((seconds / UNIT_SECONDS[usable]) * 1000) / 1000,
      ),
      unit: usable,
    };
  }
  duration(amountText: string, unit: string) {
    if (!amountText.trim()) return this.session().clear(this.spec());
    const seconds = durationSeconds(amountText, UNIT_CODE[unit] ?? "s");
    const value =
      seconds === undefined || Number.isNaN(seconds)
        ? Number(amountText) * (UNIT_SECONDS[unit] ?? 1)
        : seconds;
    if (Number.isFinite(value))
      this.session().write(this.spec(), { mode: "fixed", value });
  }
  /** A switch, a presence switch (one choice: on writes it), or Not set / Yes / No for optional booleans. */
  presence(): Choice | null {
    const choices = Array.isArray(this.spec().choices)
      ? (this.spec().choices as Choice[])
      : [];
    return choices.length === 1 ? choices[0] : null;
  }
  threeWay(): boolean {
    const spec = this.spec();
    return !spec.required && spec.default === undefined && !this.presence();
  }
  checked(): boolean {
    const presence = this.presence();
    const value = this.value();
    if (presence) return value.mode !== "absent";
    return value.mode === "fixed"
      ? value.value === true
      : this.spec().default === true;
  }
  toggle() {
    const presence = this.presence();
    if (presence)
      return this.session().write(
        this.spec(),
        this.checked() ? ABSENT : { mode: "fixed", value: presence.value },
      );
    this.session().write(this.spec(), {
      mode: "fixed",
      value: !this.checked(),
    });
  }
  threeWayValue(): string {
    const value = this.fixedValue();
    return value === true ? "yes" : value === false ? "no" : "";
  }
  chooseThreeWay(choice: string) {
    this.session().write(
      this.spec(),
      choice === "yes"
        ? { mode: "fixed", value: true }
        : choice === "no"
          ? { mode: "fixed", value: false }
          : ABSENT,
    );
  }
  // --------------------------------------------------------- choices
  loadChoices(): Choice[] {
    const session = this.session();
    const spec = this.spec();
    const choices = spec.choices;
    const descriptor = canonicalJson(spec);
    if (this.lifetime.destroyed || !session.isCurrent()) {
      this.loadedChoices = null;
      this.choices = [];
      this.choicesError = "";
      this.choicesLoading = false;
      return [];
    }
    if (Array.isArray(choices)) {
      this.loadedChoices = null;
      this.choices = [];
      this.choicesError = "";
      this.choicesLoading = false;
      return choices;
    }
    const cached = this.loadedChoices;
    if (
      !cached ||
      cached.session !== session ||
      cached.provider !== choices ||
      cached.descriptor !== descriptor ||
      cached.profile !== session.host.profile ||
      cached.api !== session.host.api
    ) {
      this.choices = [];
      this.choicesError = "";
      this.choicesLoading = !!choices;
      const load = {
        session,
        descriptor,
        provider: choices,
        profile: session.host.profile,
        api: session.host.api,
      };
      this.loadedChoices = load;
      const current = () =>
        !this.lifetime.destroyed &&
        session.isCurrent() &&
        this.session() === session &&
        this.loadedChoices === load &&
        session.host.profile === load.profile &&
        session.host.api === load.api &&
        this.spec().choices === choices &&
        canonicalJson(this.spec()) === descriptor;
      if (choices) {
        const failed = () => {
          if (!current()) return;
          this.choices = [];
          this.choicesLoading = false;
          this.choicesError = "Couldn't load the list.";
          session.host.refreshView();
        };
        try {
          void choices(session.controller.ndvContext(session.target)).then(
            (loaded) => {
              if (!current()) return;
              this.choices = loaded;
              this.choicesLoading = false;
              session.host.refreshView();
            },
            failed,
          );
        } catch {
          failed();
        }
      }
    }
    return this.choices;
  }
  selectOptions(): SelectOption[] {
    const spec = this.spec();
    return this.loadChoices().map((choice, index) => ({
      value: String(index),
      label: `${choice.label}${spec.default !== undefined && JSON.stringify(choice.value) === JSON.stringify(spec.default) ? " (default)" : ""}`,
      description: choice.disabled ?? choice.description,
      group: choice.group,
      disabled: !!choice.disabled,
    }));
  }
  selectedIndex(): string {
    const value = this.fixedValue() ?? this.spec().default;
    const index = this.loadChoices().findIndex(
      (choice) => JSON.stringify(choice.value) === JSON.stringify(value),
    );
    return index < 0 ? "" : String(index);
  }
  choose(index: string) {
    const choice = this.loadChoices()[Number(index)];
    if (choice && !choice.disabled)
      this.session().write(this.spec(), { mode: "fixed", value: choice.value });
  }
  segmented(): boolean {
    const choices = this.loadChoices();
    return (
      choices.length >= 2 &&
      choices.length <= 3 &&
      choices.every((c) => c.label.length <= 12)
    );
  }
  dateValue(): string {
    return localDateTime(this.fixedValue());
  }
  date(event: Event) {
    const stored = storedDateTime((event.target as HTMLInputElement).value);
    this.session().write(
      this.spec(),
      stored ? { mode: "fixed", value: stored } : ABSENT,
    );
  }
  json(): string {
    const value = this.fixedValue();
    return value === undefined ? "" : JSON.stringify(value, null, 2);
  }
  jsonProblem = "";
  jsonText(event: Event) {
    const text = (event.target as HTMLTextAreaElement).value;
    if (!text.trim()) {
      this.jsonProblem = "";
      return this.session().clear(this.spec());
    }
    try {
      const value = JSON.parse(text) as Json;
      this.jsonProblem = "";
      this.session().write(this.spec(), { mode: "fixed", value });
    } catch (error) {
      this.jsonProblem = `This isn't valid JSON: ${(error as Error).message}`;
    }
  }
}
