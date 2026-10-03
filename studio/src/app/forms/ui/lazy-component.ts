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
// Renders a component that is loaded on first use, with inputs and output
// handlers. The forms use it where a static import would tie modules
// together: the schema form and the property grid embed each other's
// editors, and the schema designer (which needs the dialog service) loads
// only when a schema field is shown. Each loader is a dynamic import, so the
// component lands in its own chunk.
import {
  ComponentRef,
  Directive,
  OnChanges,
  OnDestroy,
  Type,
  ViewContainerRef,
  inject,
  input,
} from "@angular/core";

/** Output name to handler; the handler current when the output fires is used. */
export type LazyOutputs = Record<string, (value: never) => void>;
interface Subscribable {
  subscribe(listener: (value: unknown) => void): { unsubscribe(): void };
}

/**
 * `<ng-container [weaveLazy]="load" [lazyInputs]="{...}" [lazyOutputs]="handlers" />`
 * where `load` resolves to a standalone component class.
 */
@Directive({ selector: "[weaveLazy]", standalone: true })
export class LazyComponent implements OnChanges, OnDestroy {
  weaveLazy = input.required<() => Promise<Type<unknown>>>();
  lazyInputs = input<Record<string, unknown>>({});
  lazyOutputs = input<LazyOutputs>({});
  private ref: ComponentRef<unknown> | null = null;
  private loading = false;
  private destroyed = false;
  private subscriptions: { unsubscribe(): void }[] = [];
  private container = inject(ViewContainerRef);

  ngOnChanges() {
    if (this.ref) this.apply();
    else if (!this.loading) {
      this.loading = true;
      // A chunk that fails to load (an interrupted update, say) is tried
      // again on the next change instead of leaving the field blank for good.
      this.weaveLazy()().then(
        (type) => this.create(type),
        (error: unknown) => {
          this.loading = false;
          console.error("A form editor could not be loaded.", error);
        },
      );
    }
  }
  ngOnDestroy() {
    this.destroyed = true;
    for (const subscription of this.subscriptions) subscription.unsubscribe();
    this.ref?.destroy();
  }
  private create(type: Type<unknown>) {
    if (this.destroyed) return;
    const ref = (this.ref = this.container.createComponent(type));
    const instance = ref.instance as Record<string, unknown>;
    for (const name of Object.keys(this.lazyOutputs())) {
      const emitter = instance[name] as Partial<Subscribable> | undefined;
      const subscription = emitter?.subscribe?.((value) =>
        (this.lazyOutputs()[name] as ((value: unknown) => void) | undefined)?.(
          value,
        ),
      );
      if (subscription) this.subscriptions.push(subscription);
    }
    this.apply();
    ref.changeDetectorRef.detectChanges();
  }
  private apply() {
    for (const [name, value] of Object.entries(this.lazyInputs()))
      this.ref!.setInput(name, value);
  }
}
