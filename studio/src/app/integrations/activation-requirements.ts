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
// What an activation must pin, derived from a published workflow version's
// export (`artifact.executable`), and which environment releases and
// assignment bindings can satisfy it. The derivation and matching functions
// are pure; the server re-checks every pin at activation (`workers/service.py`
// admit and `human_tasks/service.py` pin_assignments), so they only
// preselect. The picker component at the end of this file is created lazily
// by the activation dialog, so none of this enters the initial bundle.
//
// Field names were verified against a compiled artifact (Python probe):
// executable.dependencies[] = {kind, reference, digest, document};
// Action document.spec.implementation = {kind: "connector", uses, action} or
// {kind: "worker", taskType, taskVersion}; Connector document.spec.adapter;
// executable.graph.nodes[] humanTask nodes carry `assignment`; releases are
// serialized by alias: capabilities[] {taskType, taskVersion} and
// connector_bindings[] {connector_digest, action, adapter, task_reference}.
import {
  ChangeDetectorRef,
  Component,
  ElementRef,
  Injector,
  OnInit,
  afterNextRender,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { ApiError, type StudioApi } from "../api";

/** A workflow connection slot (`spec.connections`). */
export interface RequirementSlot {
  name: string;
  connector: string;
  required: boolean;
}
/** One Connector the workflow's connector Actions run on. */
export interface ConnectorRequirement {
  /** Connector reference, such as weave-http@2.0.0. */
  reference: string;
  /** The Connector definition digest the workflow was compiled against; "" when unknown. */
  digest: string;
  adapter: string;
  /** Descriptor actions used (read, write); empty when unknown. */
  actions: string[];
  /** Action references that need this Connector. */
  usedBy: string[];
}
/** One worker task type the workflow's worker Actions need. */
export interface WorkerRequirement {
  taskType: string;
  versions: string[];
  usedBy: string[];
}
export interface ActivationRequirements {
  /** "artifact": exact, from the compiled export; "workflow": best effort from the editor. */
  source: "artifact" | "workflow";
  connectors: ConnectorRequirement[];
  workers: WorkerRequirement[];
  assignments: string[];
  slots: RequirementSlot[];
}
export interface ReleaseCapability {
  taskType: string;
  taskVersion: string;
}
export interface ReleaseBinding {
  connector_digest: string;
  action: string;
  adapter: string;
  task_reference: string;
}
export interface ReleaseView {
  id: string;
  created_at?: string;
  image_digest: string;
  capabilities: ReleaseCapability[];
  connector_bindings: ReleaseBinding[];
}
export interface AssignmentBindingView {
  binding_id: string;
  name: string;
  enabled: boolean;
  revision: number;
  principals: number;
  groups: number;
}
export interface ConnectorVersionView {
  id: string;
  name: string;
  version: string;
  definition_digest: string;
  retired: boolean;
}
/** The fields of a connector-descriptors read that activation and readiness use. */
export interface DescriptorView {
  adapter: string;
  reference: string;
  digest: string;
  published_version_id: string | null;
}
export type ConnectorVersionMatch =
  | { id: string; via: "descriptor" | "catalog" }
  | { problem: "not-published" | "installed-mismatch" };
export interface PinChoices {
  /** Connector version ID per requirement key (see connectorKey). */
  connectorVersions: Record<string, string>;
  /** Release ID per requirement key. */
  connectorReleases: Record<string, string>;
  /** Release ID per worker task type. */
  workers: Record<string, string>;
  /** Assignment binding ID per assignment name. */
  assignments: Record<string, string>;
}
export interface ActivationPinMaps {
  connector_release_ids: Record<string, string>;
  worker_release_ids: Record<string, string>;
  assignment_binding_ids: Record<string, string>;
}

export const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const digestPattern = /^[0-9a-f]{64}$/;
const namePattern = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

type Json = Record<string, unknown>;
const record = (value: unknown): Json =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Json)
    : {};
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const text = (value: unknown): string =>
  typeof value === "string" ? value : "";
const unique = (values: string[]) => [...new Set(values)].sort();

/** The stable key of a connector requirement: its digest, or its reference when unresolved. */
export const connectorKey = (requirement: ConnectorRequirement) =>
  requirement.digest || requirement.reference;

/**
 * Reads a `definitions.export` response of a published workflow version.
 * Returns null when the response does not carry a compiled executable, so
 * the caller can fall back to what the editor knows.
 */
export function requirementsFromExport(
  exported: unknown,
): ActivationRequirements | null {
  const executable = record(record(record(exported)["artifact"])["executable"]);
  const dependencies = executable["dependencies"];
  if (!Array.isArray(dependencies)) return null;
  const connectorsByReference = new Map<
    string,
    { digest: string; adapter: string }
  >();
  for (const item of dependencies.map(record))
    if (
      item["kind"] === "Connector" &&
      digestPattern.test(text(item["digest"]))
    )
      connectorsByReference.set(text(item["reference"]), {
        digest: text(item["digest"]),
        adapter: text(record(record(item["document"])["spec"])["adapter"]),
      });
  const connectors = new Map<string, ConnectorRequirement>();
  const workers = new Map<string, WorkerRequirement>();
  for (const item of dependencies.map(record)) {
    if (item["kind"] !== "Action") continue;
    const reference = text(item["reference"]);
    const implementation = record(
      record(record(item["document"])["spec"])["implementation"],
    );
    if (implementation["kind"] === "connector") {
      const uses = text(implementation["uses"]);
      const connector = connectorsByReference.get(uses);
      if (!connector) continue;
      const requirement = connectors.get(connector.digest) ?? {
        reference: uses,
        digest: connector.digest,
        adapter: connector.adapter,
        actions: [],
        usedBy: [],
      };
      requirement.actions = unique([
        ...requirement.actions,
        text(implementation["action"]),
      ]).filter(Boolean);
      requirement.usedBy = unique([...requirement.usedBy, reference]);
      connectors.set(connector.digest, requirement);
    } else if (implementation["kind"] === "worker") {
      const taskType = text(implementation["taskType"]);
      if (!namePattern.test(taskType)) continue;
      const requirement = workers.get(taskType) ?? {
        taskType,
        versions: [],
        usedBy: [],
      };
      requirement.versions = unique([
        ...requirement.versions,
        text(implementation["taskVersion"]),
      ]).filter(Boolean);
      requirement.usedBy = unique([...requirement.usedBy, reference]);
      workers.set(taskType, requirement);
    }
  }
  const assignments = unique(
    list(record(executable["graph"])["nodes"])
      .map(record)
      .filter((node) => node["kind"] === "humanTask")
      .map((node) => text(node["assignment"]))
      .filter((name) => namePattern.test(name)),
  );
  const declared =
    executable["connections"] ??
    record(record(record(exported)["document"])["spec"])["connections"];
  return {
    source: "artifact",
    connectors: [...connectors.values()].sort((a, b) =>
      a.reference.localeCompare(b.reference),
    ),
    workers: [...workers.values()].sort((a, b) =>
      a.taskType.localeCompare(b.taskType),
    ),
    assignments,
    slots: slotsFrom(declared),
  };
}

function slotsFrom(value: unknown): RequirementSlot[] {
  return Object.entries(record(value))
    .map(([name, requirement]) => ({
      name,
      connector: text(record(requirement)["connector"]),
      required: record(requirement)["required"] !== false,
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Best-effort requirements from the open editor when no export is available:
 * connectors come from the connection slots (digests are resolved later from
 * the project's published Connectors), worker task types and assignments from
 * the steps the editor knows about.
 */
export function requirementsFromWorkflow(
  slots: RequirementSlot[],
  workerTaskTypes: string[],
  assignments: string[],
): ActivationRequirements {
  const connectors = unique(slots.map((slot) => slot.connector))
    .filter(Boolean)
    .map((reference) => ({
      reference,
      digest: "",
      adapter: "",
      actions: [],
      usedBy: slots
        .filter((slot) => slot.connector === reference)
        .map((slot) => `slot ${slot.name}`),
    }));
  return {
    source: "workflow",
    connectors,
    workers: unique(workerTaskTypes)
      .filter((taskType) => namePattern.test(taskType))
      .map((taskType) => ({ taskType, versions: [], usedBy: [] })),
    assignments: unique(assignments).filter((name) => namePattern.test(name)),
    slots: [...slots],
  };
}

/** Releases from a `releases.list` page; malformed entries are dropped. */
export function readReleases(items: unknown): ReleaseView[] {
  return list(items)
    .map(record)
    .filter((item) => uuidPattern.test(text(item["id"])))
    .map((item) => ({
      id: text(item["id"]),
      ...(typeof item["created_at"] === "string"
        ? { created_at: item["created_at"] }
        : {}),
      image_digest: text(item["image_digest"]),
      capabilities: list(item["capabilities"])
        .map(record)
        .map((capability) => ({
          taskType: text(capability["taskType"] ?? capability["task_type"]),
          taskVersion: text(
            capability["taskVersion"] ?? capability["task_version"],
          ),
        })),
      connector_bindings: list(item["connector_bindings"])
        .map(record)
        .map((binding) => ({
          connector_digest: text(binding["connector_digest"]),
          action: text(binding["action"]),
          adapter: text(binding["adapter"]),
          task_reference: text(binding["task_reference"]),
        })),
    }));
}

/** Assignment bindings from `human_assignments.list`. */
export function readAssignmentBindings(
  items: unknown,
): AssignmentBindingView[] {
  return list(items)
    .map(record)
    .filter((item) => uuidPattern.test(text(item["binding_id"])))
    .map((item) => ({
      binding_id: text(item["binding_id"]),
      name: text(item["name"]),
      enabled: item["enabled"] === true,
      revision: Number(item["revision"]) || 0,
      principals: list(item["principal_ids"]).length,
      groups: list(item["group_ids"]).length,
    }));
}

/** Published Connector versions from a `definitions.list` page of the connectors collection. */
export function readConnectorVersions(items: unknown): ConnectorVersionView[] {
  return list(items)
    .map(record)
    .filter(
      (item) =>
        uuidPattern.test(text(item["id"])) &&
        !item["unavailable"] &&
        (item["kind"] === undefined || item["kind"] === "Connector"),
    )
    .map((item) => ({
      id: text(item["id"]),
      name: text(item["name"]),
      version: text(item["version"]),
      definition_digest: text(item["definition_digest"]),
      retired: item["retired"] === true,
    }));
}

/** The fields of a `connector_descriptors.read` response, or null when it is not one. */
export function readDescriptor(value: unknown): DescriptorView | null {
  const item = record(value);
  const digest = text(item["digest"]);
  if (!digestPattern.test(digest)) return null;
  const published = text(item["published_version_id"]);
  return {
    adapter: text(item["adapter"]),
    reference: text(item["reference"]),
    digest,
    published_version_id: uuidPattern.test(published) ? published : null,
  };
}

/** Resolves an unresolved (slot-derived) requirement against the published Connectors. */
export function resolveConnector(
  requirement: ConnectorRequirement,
  versions: ConnectorVersionView[],
): ConnectorRequirement {
  if (requirement.digest) return requirement;
  const found = versions.find(
    (version) =>
      !version.retired &&
      `${version.name}@${version.version}` === requirement.reference &&
      digestPattern.test(version.definition_digest),
  );
  return found
    ? { ...requirement, digest: found.definition_digest }
    : requirement;
}

/**
 * The Connector version ID the activation pins for a requirement. The server
 * matches `connector_release_ids` keys by the version's definition digest.
 */
export function connectorVersionFor(
  requirement: ConnectorRequirement,
  descriptor: DescriptorView | null,
  versions: ConnectorVersionView[],
): ConnectorVersionMatch {
  if (descriptor && descriptor.digest === requirement.digest) {
    if (descriptor.published_version_id)
      return { id: descriptor.published_version_id, via: "descriptor" };
  }
  const found = versions.find(
    (version) =>
      !version.retired &&
      requirement.digest !== "" &&
      version.definition_digest === requirement.digest,
  );
  if (found) return { id: found.id, via: "catalog" };
  if (
    descriptor &&
    requirement.digest &&
    descriptor.digest !== requirement.digest
  )
    return { problem: "installed-mismatch" };
  return { problem: "not-published" };
}

const offers = (release: ReleaseView, reference: string) =>
  release.capabilities.some(
    (capability) =>
      `${capability.taskType}@${capability.taskVersion}` === reference,
  );

/**
 * Releases that can run a connector requirement: a binding with the exact
 * Connector digest for every action used, whose task capability the release
 * offers. With unknown actions (slot-derived), any binding for the digest.
 */
export function connectorReleaseCandidates(
  requirement: ConnectorRequirement,
  releases: ReleaseView[],
): ReleaseView[] {
  if (!requirement.digest) return [];
  return releases.filter((release) => {
    const bindings = release.connector_bindings.filter(
      (binding) =>
        binding.connector_digest === requirement.digest &&
        offers(release, binding.task_reference),
    );
    return requirement.actions.length
      ? requirement.actions.every((action) =>
          bindings.some((binding) => binding.action === action),
        )
      : bindings.length > 0;
  });
}

/** Releases offering every required version of a worker task type. */
export function workerReleaseCandidates(
  requirement: WorkerRequirement,
  releases: ReleaseView[],
): ReleaseView[] {
  return releases.filter((release) =>
    requirement.versions.length
      ? requirement.versions.every((version) =>
          offers(release, `${requirement.taskType}@${version}`),
        )
      : release.capabilities.some(
          (capability) => capability.taskType === requirement.taskType,
        ),
  );
}

/** Enabled assignment bindings with exactly the assignment's name. */
export function assignmentCandidates(
  name: string,
  bindings: AssignmentBindingView[],
): AssignmentBindingView[] {
  return bindings.filter((binding) => binding.enabled && binding.name === name);
}

/** The single candidate's ID, or "" when there are none or several. */
export function onlyChoice(ids: string[]): string {
  return ids.length === 1 ? ids[0] : "";
}

/** A plain label for a release in a picker. */
export function releaseLabel(release: ReleaseView): string {
  const versions = unique(
    release.capabilities
      .map((capability) => capability.taskVersion)
      .filter(Boolean),
  );
  const date =
    release.created_at &&
    /^\d{4}-\d{2}-\d{2}T/.test(release.created_at) &&
    Number.isFinite(Date.parse(release.created_at))
      ? release.created_at.slice(0, 10)
      : "";
  return `${versions.length ? `Version${versions.length > 1 ? "s" : ""} ${versions.join(", ")}` : "Version unavailable"}${date ? ` · ${date}` : ""}`;
}

/** A plain label for an assignment binding in a picker. */
export function assignmentLabel(binding: AssignmentBindingView): string {
  const people = [
    binding.principals
      ? `${binding.principals} ${binding.principals === 1 ? "person" : "people"}`
      : "",
    binding.groups
      ? `${binding.groups} ${binding.groups === 1 ? "group" : "groups"}`
      : "",
  ]
    .filter(Boolean)
    .join(", ");
  return `${binding.name} · revision ${binding.revision}${people ? ` · ${people}` : ""}`;
}

/**
 * Turns the chosen pins into the activation request maps. Every requirement
 * must be pinned with IDs; the problem names the first missing one in plain
 * language. For editor-derived requirements, a connector or worker without a
 * chosen release and an assignment without a binding are left out.
 */
export function activationPins(
  requirements: ActivationRequirements,
  choices: PinChoices,
): ActivationPinMaps | { problem: string } {
  const pins: ActivationPinMaps = {
    connector_release_ids: {},
    worker_release_ids: {},
    assignment_binding_ids: {},
  };
  const example = "0f8fad5b-d9cb-469f-a165-70867728950e";
  for (const requirement of requirements.connectors) {
    const key = connectorKey(requirement);
    const version = (choices.connectorVersions[key] ?? "").trim();
    const release = (choices.connectorReleases[key] ?? "").trim();
    // From the editor a slot may serve a worker, so an unchosen release is no pin.
    if (!release && requirements.source === "workflow") continue;
    if (!uuidPattern.test(version) || !uuidPattern.test(release))
      return {
        problem: `Choose a connector release for ${requirement.reference}. It needs the published connector version ID and a release ID, such as ${example}.`,
      };
    if (pins.connector_release_ids[version])
      return {
        problem: `${requirement.reference} is pinned twice. Each published connector version takes one release.`,
      };
    pins.connector_release_ids[version] = release;
  }
  for (const requirement of requirements.workers) {
    const release = (choices.workers[requirement.taskType] ?? "").trim();
    if (!release && requirements.source === "workflow") continue;
    if (!uuidPattern.test(release))
      return {
        problem: `Choose a worker release for ${requirement.taskType}, such as ${example}.`,
      };
    pins.worker_release_ids[requirement.taskType] = release;
  }
  for (const name of requirements.assignments) {
    const binding = (choices.assignments[name] ?? "").trim();
    if (!binding && requirements.source === "workflow") continue;
    if (!uuidPattern.test(binding))
      return {
        problem: `Choose who receives the ${name} tasks: an assignment binding ID, such as ${example}.`,
      };
    pins.assignment_binding_ids[name] = binding;
  }
  return pins;
}

/** One pin to choose in the picker. */
interface PinRow {
  kind: "connector" | "worker" | "assignment";
  key: string;
  label: string;
  detail: string;
  options: { id: string; label: string; details?: string }[];
  value: string;
  /** Connector rows: the published connector version the release is pinned for. */
  versionId: string;
  /** Connector rows: the version could not be resolved and must be typed. */
  typeVersion: boolean;
  /** A worker or assignment row added by hand; its key is editable. */
  custom: boolean;
  note: string;
}
type Lookup<T> = { ok: true; value: T } | { ok: false; status: number };
const maximumPages = 4;
const pinGroups = [
  ["connector", "Integration versions"],
  ["worker", "Task versions"],
  ["assignment", "People and teams"],
] as const;
let pickerSequence = 0;

async function lookup<T>(read: () => Promise<T>): Promise<Lookup<T>> {
  try {
    return { ok: true, value: await read() };
  } catch (e) {
    return { ok: false, status: e instanceof ApiError ? e.status : 0 };
  }
}

/**
 * The connector release, worker release and assignment binding pickers of
 * the activation dialog. With `versionId` it reads the published version's
 * export, so the rows are exactly what the server checks and a unique match
 * is preselected; otherwise the rows come from what the editor knows and
 * extra rows can be added by hand. Free-text IDs remain the fallback when a
 * list is refused or empty. `collect()` returns the request maps.
 */
@Component({
  selector: "weave-activation-pins",
  standalone: true,
  styles: `
    :host {
      display: block;
    }
    h3 {
      margin: 22px 0 4px;
    }
    .binding-row > select,
    .binding-row > input {
      display: block;
      width: 100%;
      margin-top: 6px;
    }
    .binding-row > .hint {
      margin: 4px 0 0;
    }
  `,
  template: `@if (loading()) {
      <p class="dialog-status" role="status">
        <span class="loading-spinner small"></span>Finding the releases and
        assignments this version needs…
      </p>
    } @else {
      @if (notice()) {
        <p class="hint">{{ notice() }}</p>
      }
      @for (group of groups; track group[0]) {
        @if (rowsOf(group[0]).length) {
          <h3>{{ group[1] }}</h3>
          <div class="binding-list">
            @for (row of rowsOf(group[0]); track $index) {
              <div class="binding-row">
                @if (row.custom) {
                  <input
                    [id]="pinId(row, 'key')"
                    [attr.aria-label]="
                      row.kind === 'worker'
                        ? 'Worker task type'
                        : 'Assignment name'
                    "
                    [placeholder]="
                      row.kind === 'worker' ? 'Task type' : 'Assignment'
                    "
                    autocomplete="off"
                    [value]="row.key"
                    (input)="row.key = text($event); changed.emit()"
                  />
                } @else {
                  <label [for]="pinId(row, 'value')"
                    >{{ row.label }} <small>{{ row.detail }}</small></label
                  >
                }
                @if (row.typeVersion) {
                  <input
                    [attr.aria-label]="
                      'Published connector version ID for ' + row.label
                    "
                    placeholder="Connector version ID"
                    autocomplete="off"
                    [value]="row.versionId"
                    (input)="row.versionId = text($event); changed.emit()"
                  />
                }
                @if (row.options.length) {
                  <select
                    [id]="pinId(row, 'value')"
                    [attr.aria-describedby]="
                      row.note ? pinId(row, 'note') : null
                    "
                    (change)="row.value = text($event); changed.emit()"
                  >
                    <option value="" [selected]="!row.value">
                      {{
                        row.kind === "assignment"
                          ? "Choose an assignment binding"
                          : "Choose a release"
                      }}
                    </option>
                    @for (option of row.options; track option.id) {
                      <option
                        [value]="option.id"
                        [selected]="row.value === option.id"
                      >
                        {{ option.label }}
                      </option>
                    }
                  </select>
                } @else {
                  <input
                    [id]="pinId(row, 'value')"
                    [attr.aria-label]="row.custom ? idLabel(row) : null"
                    [attr.aria-describedby]="
                      row.note ? pinId(row, 'note') : null
                    "
                    [placeholder]="idLabel(row)"
                    autocomplete="off"
                    [value]="row.value"
                    (input)="row.value = text($event); changed.emit()"
                  />
                }
                @if (row.note) {
                  <p class="hint" [id]="pinId(row, 'note')">{{ row.note }}</p>
                }
                @if (row.options.length) {
                  <details class="release-details">
                    <summary>Technical details</summary>
                    @for (option of row.options; track option.id) {
                      <p>
                        {{ option.label }}<br /><code>{{ option.id }}</code
                        ><br /><code>{{ option.details }}</code>
                      </p>
                    }
                  </details>
                }
                @if (row.custom) {
                  <button type="button" (click)="remove(row)">Remove</button>
                }
              </div>
            }
          </div>
        }
      }
      @if (source() === "workflow") {
        <div class="dialog-actions">
          <button
            type="button"
            [id]="prefix + '-add-worker'"
            (click)="add('worker')"
          >
            Add worker release
          </button>
          <button
            type="button"
            [id]="prefix + '-add-assignment'"
            (click)="add('assignment')"
          >
            Add assignment binding
          </button>
        </div>
      }
    }`,
})
export class ActivationPinsPicker implements OnInit {
  api = input.required<StudioApi>();
  /** The published workflow version ID; with it the pins are exact. */
  versionId = input("");
  slots = input<RequirementSlot[]>([]);
  workerTaskTypes = input<string[]>([]);
  assignments = input<string[]>([]);
  /** Any pin changed, so the host can clear its problem message. */
  changed = output<void>();
  /**
   * The published version's connection slots once its export is read (null
   * when the pins come from the editor), so the host binds exactly the slots
   * the server checks even after the workflow was edited since publishing.
   */
  exactSlots = signal<RequirementSlot[] | null>(null);
  loading = signal(true);
  source = signal<"artifact" | "workflow">("workflow");
  rows = signal<PinRow[]>([]);
  reviewAutomatic = signal(false);
  automaticRows() {
    return this.rows().filter((row) => this.automatic(row));
  }
  private automatic(row: PinRow) {
    return (
      !row.custom &&
      !row.typeVersion &&
      row.options.length === 1 &&
      row.value === row.options[0].id
    );
  }
  notice = signal("");
  readonly groups = pinGroups;
  readonly prefix = `pin-${++pickerSequence}`;
  private requirements: ActivationRequirements | null = null;
  private cdr = inject(ChangeDetectorRef);
  private host = inject<ElementRef<HTMLElement>>(ElementRef);
  private injector = inject(Injector);
  ngOnInit() {
    void this.load();
  }
  /** Moves focus once the rows have rendered (an added row, or the Add button after a removal). */
  private focusSoon(id: string) {
    afterNextRender(
      () =>
        this.host.nativeElement.querySelector<HTMLElement>(`#${id}`)?.focus(),
      { injector: this.injector },
    );
  }
  text(event: Event) {
    return (event.target as HTMLInputElement).value.trim();
  }
  rowsOf(kind: PinRow["kind"]) {
    return this.rows().filter(
      (row) =>
        row.kind === kind && (this.reviewAutomatic() || !this.automatic(row)),
    );
  }
  pinId(row: PinRow, part: string) {
    return `${this.prefix}-${this.rows().indexOf(row)}-${part}`;
  }
  idLabel(row: PinRow) {
    return row.kind === "assignment" ? "Assignment binding ID" : "Release ID";
  }
  add(kind: "worker" | "assignment") {
    const index = this.rows().length;
    this.rows.update((rows) => [
      ...rows,
      {
        kind,
        key: "",
        label: "",
        detail: "",
        options: [],
        value: "",
        versionId: "",
        typeVersion: false,
        custom: true,
        note: "",
      },
    ]);
    this.focusSoon(`${this.prefix}-${index}-key`);
  }
  remove(row: PinRow) {
    this.rows.update((rows) => rows.filter((r) => r !== row));
    this.changed.emit();
    // The removed row held focus; keep it in the dialog so Escape and Tab work.
    this.focusSoon(`${this.prefix}-add-${row.kind}`);
  }
  /** The activation request maps, or the first problem in plain language. */
  collect(): ActivationPinMaps | { problem: string } {
    const rows = this.rows();
    const pick = (kind: PinRow["kind"], field: "value" | "versionId") =>
      Object.fromEntries(
        rows
          .filter((row) => row.kind === kind && !row.custom)
          .map((row) => [row.key, row[field].trim()]),
      );
    const pins = this.requirements
      ? activationPins(this.requirements, {
          connectorVersions: pick("connector", "versionId"),
          connectorReleases: pick("connector", "value"),
          workers: pick("worker", "value"),
          assignments: pick("assignment", "value"),
        })
      : {
          connector_release_ids: {},
          worker_release_ids: {},
          assignment_binding_ids: {},
        };
    if ("problem" in pins) return pins;
    // Rows added by hand: a name and an ID each, or left empty.
    for (const row of rows.filter((r) => r.custom)) {
      const key = row.key.trim(),
        value = row.value.trim();
      if (!key && !value) continue;
      if (!namePattern.test(key) || !uuidPattern.test(value))
        return {
          problem: `Each ${row.kind === "worker" ? "worker release" : "assignment binding"} needs a name and an ID such as 0f8fad5b-d9cb-469f-a165-70867728950e.`,
        };
      (row.kind === "worker"
        ? pins.worker_release_ids
        : pins.assignment_binding_ids)[key] = value;
    }
    return pins;
  }
  /** Reads what this version must pin and preselects the unique matches. */
  async load() {
    this.loading.set(true);
    try {
      const api = this.api();
      let requirements: ActivationRequirements | null = null;
      if (this.versionId()) {
        const exported = await lookup(() =>
          api.request<unknown>(
            `${api.project}/workflows/${encodeURIComponent(this.versionId())}/export`,
          ),
        );
        requirements = exported.ok
          ? requirementsFromExport(exported.value)
          : null;
        if (!requirements)
          this.notice.set(
            "Studio couldn't read this version's requirements, so these pins come from the editor. The platform checks them when you activate.",
          );
      }
      requirements ??= requirementsFromWorkflow(
        this.slots(),
        this.workerTaskTypes(),
        this.assignments(),
      );
      const exact = requirements.source === "artifact";
      const collectPages = <T>(
        read: (items: unknown) => T[],
        collection: string,
        environment: boolean,
      ): Promise<Lookup<T[]>> =>
        lookup(async () => {
          const items: T[] = [];
          let cursor: string | undefined;
          for (let index = 0; index < maximumPages; index++) {
            const result = await api.page(collection, environment, cursor);
            items.push(...read(result.items));
            if (!result.next_cursor) break;
            cursor = result.next_cursor;
          }
          return items;
        });
      const none = <T>(): Promise<Lookup<T[]>> =>
        Promise.resolve({ ok: true, value: [] });
      const adapters = [
        ...new Set(requirements.connectors.map((c) => c.adapter)),
      ].filter(Boolean);
      const [releases, bindings, versions, descriptors] = await Promise.all([
        requirements.connectors.length || requirements.workers.length
          ? collectPages(readReleases, "worker-releases", true)
          : none<ReleaseView>(),
        requirements.assignments.length
          ? lookup(async () =>
              readAssignmentBindings(
                (
                  await api.request<{ items: unknown }>(
                    `${api.environment}/human-assignments`,
                  )
                ).items,
              ),
            )
          : none<AssignmentBindingView>(),
        requirements.connectors.length
          ? collectPages(readConnectorVersions, "connectors", false)
          : none<ConnectorVersionView>(),
        Promise.all(
          adapters.map(async (adapter) => {
            const read = await lookup(() =>
              api.request<unknown>(
                `${api.project}/connector-descriptors/${encodeURIComponent(adapter)}`,
              ),
            );
            return read.ok ? readDescriptor(read.value) : null;
          }),
        ),
      ]);
      const releaseList = releases.ok ? releases.value : [];
      const versionList = versions.ok ? versions.value : [];
      const releaseNote = (count: number, preselect: boolean, what: string) =>
        !releases.ok
          ? releases.status === 403
            ? "Your account can't list releases here. Enter the release ID your platform operator gives you."
            : "Studio couldn't list releases. Enter the release ID, or try again later."
          : !count
            ? `No release in this environment ${what}. A platform operator registers one.`
            : count === 1 && preselect
              ? `Selected the only release that ${what}.`
              : "";
      const option = (release: ReleaseView) => ({
        id: release.id,
        label: releaseLabel(release),
        details: release.image_digest,
      });
      const used = (usedBy: string[], otherwise: string) =>
        usedBy.length ? `Used by ${usedBy.join(", ")}` : otherwise;
      const rows: PinRow[] = [];
      const kept: ActivationRequirements = { ...requirements, connectors: [] };
      for (const original of requirements.connectors) {
        const requirement = resolveConnector(original, versionList);
        // From the editor, only connectors published here can need a pin.
        if (!exact && !requirement.digest) continue;
        kept.connectors.push(requirement);
        const descriptor =
          descriptors.find(
            (d): d is DescriptorView =>
              !!d && d.adapter === requirement.adapter,
          ) ?? null;
        const match = connectorVersionFor(requirement, descriptor, versionList);
        const candidates = connectorReleaseCandidates(requirement, releaseList);
        const versionId = "id" in match ? match.id : "";
        rows.push({
          kind: "connector",
          key: connectorKey(requirement),
          label: requirement.reference,
          detail: used(requirement.usedBy, ""),
          options: candidates.map(option),
          // From the editor a slot can serve a worker; never guess a pin then.
          value: exact ? onlyChoice(candidates.map((r) => r.id)) : "",
          versionId,
          typeVersion: !versionId,
          custom: false,
          note:
            "problem" in match && match.problem === "installed-mismatch"
              ? `This version was compiled against a different ${requirement.reference} than the platform has installed. Publish the workflow again, then activate the new version.`
              : !versionId && versions.ok
                ? `${requirement.reference} isn't published in this project. A developer or platform operator publishes it first.`
                : releaseNote(
                    candidates.length,
                    exact,
                    `runs ${requirement.reference}`,
                  ),
        });
      }
      for (const requirement of requirements.workers) {
        const candidates = workerReleaseCandidates(requirement, releaseList);
        rows.push({
          kind: "worker",
          key: requirement.taskType,
          label: requirement.taskType,
          detail: used(requirement.usedBy, "Worker task type"),
          options: candidates.map(option),
          value: onlyChoice(candidates.map((r) => r.id)),
          versionId: "",
          typeVersion: false,
          custom: false,
          note: releaseNote(
            candidates.length,
            true,
            `offers ${requirement.taskType}`,
          ),
        });
      }
      for (const name of requirements.assignments) {
        const candidates = bindings.ok
          ? assignmentCandidates(name, bindings.value)
          : [];
        rows.push({
          kind: "assignment",
          key: name,
          label: name,
          detail: "Who receives these human tasks",
          options: candidates.map((b) => ({
            id: b.binding_id,
            label: assignmentLabel(b),
          })),
          value: onlyChoice(candidates.map((b) => b.binding_id)),
          versionId: "",
          typeVersion: false,
          custom: false,
          note: !bindings.ok
            ? bindings.status === 403
              ? "Your account can't list assignment bindings. Enter the binding ID a task manager gives you."
              : "Studio couldn't list assignment bindings. Enter the binding ID, or try again later."
            : candidates.length === 1
              ? "Selected the only enabled binding with this name."
              : candidates.length
                ? ""
                : `No enabled assignment binding named ${name} exists here. A task manager creates one.`,
        });
      }
      this.requirements = kept;
      this.source.set(requirements.source);
      this.exactSlots.set(exact ? requirements.slots : null);
      this.rows.set(rows);
    } catch {
      // The dialog still works with typed IDs; the platform checks every pin.
      this.requirements = null;
      this.source.set("workflow");
      this.rows.set([]);
      this.notice.set(
        "Studio couldn't look up releases for this version. Add any pins the platform asks for.",
      );
    } finally {
      this.loading.set(false);
      this.cdr.markForCheck();
    }
  }
}
