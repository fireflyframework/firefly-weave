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
// The inspector's searchable action picker over the project's published
// catalog (WP-19). It turns definitions.list rows, enriched with any action
// contracts the editor already loaded, into picker entries. Load it lazily
// (@defer): the shell keeps the catalog and its paging.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import {
  ActionPicker,
  actionPickerItem,
  type ActionPickerChoice,
  type ActionPickerItem,
} from "./action-picker";
import type { EditorHost } from "./editor-host";

/** Picker entries for catalog rows; retired or unreadable rows are left out. */
export function catalogItems(
  rows: readonly Record<string, unknown>[],
  contracts: ReadonlyMap<string, Record<string, unknown>>,
): ActionPickerItem[] {
  return rows.flatMap((row) => {
    const item = actionPickerItem(
      row,
      contracts.get(`${String(row["name"])}@${String(row["version"])}`),
    );
    return item ? [item] : [];
  });
}

@Component({
  selector: "weave-catalog-picker",
  // A view of the shell's state: checked whenever the shell renders.
  changeDetection: ChangeDetectionStrategy.Eager,
  standalone: true,
  imports: [ActionPicker],
  template: `<weave-action-picker
    [actions]="items()"
    [value]="value()"
    [label]="label()"
    [loading]="host().catalogState === 'loading'"
    [hasMore]="!!host().actionNextCursor"
    [loadingMore]="host().catalogAppending"
    [canCreate]="host().can('definition.publish')"
    [disabled]="disabled()"
    (choose)="choose.emit($event)"
    (createAction)="host().openApiBuilder('step')"
    (loadMore)="host().loadActionCatalog(true)"
    (retry)="host().loadActionCatalog(false, true)"
  />`,
})
export class CatalogPicker {
  /** The editor shell, which owns the catalog and its paging. */
  host = input.required<EditorHost>();
  /** The `uses` reference the step has now. */
  value = input<string | null>(null);
  label = input("Published action");
  disabled = input(false);
  choose = output<ActionPickerChoice>();

  private memo: {
    rows: unknown;
    contracts: unknown;
    items: ActionPickerItem[];
  } | null = null;
  /** Picker entries, rebuilt when the catalog or the loaded contracts change. */
  items() {
    const host = this.host();
    if (
      this.memo?.rows !== host.actionVersions ||
      this.memo.contracts !== host.catalogContracts
    )
      this.memo = {
        rows: host.actionVersions,
        contracts: host.catalogContracts,
        items: catalogItems(host.actionVersions, host.catalogContracts),
      };
    return this.memo.items;
  }
}
