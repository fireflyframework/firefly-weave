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
import "@angular/compiler";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastService, toastDuration } from "../src/app/toast";

describe("toasts", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("shows one toast at a time and closes it after 6 seconds", () => {
    const toasts = new ToastService();
    toasts.show({ text: "Task claimed." });
    toasts.show({ text: "Task released." });
    expect(toasts.current()?.text).toBe("Task released.");
    expect(toasts.current()?.tone).toBe("neutral");
    vi.advanceTimersByTime(toastDuration - 1);
    expect(toasts.current()).not.toBeNull();
    vi.advanceTimersByTime(1);
    expect(toasts.current()).toBeNull();
  });

  it("waits while hovered or focused", () => {
    const toasts = new ToastService();
    toasts.show({ text: "Run started." });
    toasts.hold("hover");
    toasts.hold("focus");
    vi.advanceTimersByTime(toastDuration * 3);
    expect(toasts.current()?.text).toBe("Run started.");
    toasts.release("hover");
    vi.advanceTimersByTime(toastDuration * 3);
    expect(toasts.current()).not.toBeNull();
    toasts.release("focus");
    vi.advanceTimersByTime(toastDuration);
    expect(toasts.current()).toBeNull();
  });

  it("runs its one action and closes", () => {
    const toasts = new ToastService();
    const run = vi.fn();
    toasts.show({ text: "Deleted a draft.", action: { label: "Undo", run } });
    toasts.act();
    expect(run).toHaveBeenCalledOnce();
    expect(toasts.current()).toBeNull();
    // A late timer from a replaced toast never closes the newer one.
    toasts.show({ text: "First" });
    vi.advanceTimersByTime(toastDuration - 10);
    toasts.show({ text: "Second", tone: "danger" });
    vi.advanceTimersByTime(20);
    expect(toasts.current()?.text).toBe("Second");
    expect(toasts.current()?.tone).toBe("danger");
  });
});
