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
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A fresh module per test: the pause between downloads is module state.
let exportModule: typeof import("../src/app/export-file");
const exportFile: typeof exportModule.exportFile = (...args) =>
  exportModule.exportFile(...args);
const exportNotice: typeof exportModule.exportNotice = (...args) =>
  exportModule.exportNotice(...args);
const isDesktopShell = () => exportModule.isDesktopShell();

// Vitest runs in Node here, so a minimal document stands in for the DOM.
interface Click {
  href: string;
  download: string;
  attached: boolean;
}
class FakeElement {
  children: FakeElement[] = [];
  parent: FakeElement | null = null;
  style: Record<string, string> = {};
  href = "";
  download = "";
  rel = "";
  constructor(readonly owner: FakeDocument) {}
  append(child: FakeElement) {
    child.parent = this;
    this.children.push(child);
  }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((c) => c !== this);
    this.parent = null;
  }
  click() {
    if (this.owner.failClick) throw new Error("blocked");
    this.owner.clicked.push({
      href: this.href,
      download: this.download,
      attached: this.parent !== null,
    });
  }
}
class FakeDocument {
  body = new FakeElement(this);
  clicked: Click[] = [];
  failClick = false;
  createElement() {
    return new FakeElement(this);
  }
}

let page: FakeDocument;
let minted: Blob[];
let revoked: string[];
const blob = (n: number) => `blob:http://127.0.0.1:32199/${n}`;
function install(desktop: boolean) {
  page = new FakeDocument();
  vi.stubGlobal("document", page);
  vi.stubGlobal(
    "window",
    desktop ? { __TAURI_INTERNALS__: {}, isTauri: true } : {},
  );
}
beforeEach(async () => {
  vi.useFakeTimers();
  vi.resetModules();
  exportModule = await import("../src/app/export-file");
  minted = [];
  revoked = [];
  vi.spyOn(URL, "createObjectURL").mockImplementation((file) => {
    minted.push(file as Blob);
    return blob(minted.length);
  });
  vi.spyOn(URL, "revokeObjectURL").mockImplementation((url) => {
    revoked.push(url);
  });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("exportFile", () => {
  for (const desktop of [false, true]) {
    it(`starts downloads one at a time so WebKit keeps each (${desktop ? "desktop" : "browser"})`, async () => {
      install(desktop);
      expect(
        exportFile("orders.yaml", "application/yaml", "name: orders\n"),
      ).toBe("downloaded");
      expect(
        exportFile(
          "orders.layout.json",
          "application/json",
          new TextEncoder().encode('{"a":1}'),
        ),
      ).toBe("downloaded");
      // The first download starts while the caller still holds the click.
      expect(page.clicked).toEqual([
        { href: blob(1), download: "orders.yaml", attached: true },
      ]);
      vi.advanceTimersByTime(399);
      expect(page.clicked).toHaveLength(1);
      vi.advanceTimersByTime(1);
      expect(page.clicked).toEqual([
        { href: blob(1), download: "orders.yaml", attached: true },
        { href: blob(2), download: "orders.layout.json", attached: true },
      ]);
      expect(page.body.children).toEqual([]);
      expect(minted.map((b) => b.type)).toEqual([
        "application/yaml",
        "application/json",
      ]);
      expect(await minted[0].text()).toBe("name: orders\n");
      expect(await minted[1].text()).toBe('{"a":1}');
    });
  }

  it("keeps each file until the webview has taken the download", () => {
    install(true);
    exportFile("a.yaml", "application/yaml", "a");
    exportFile("a.layout.json", "application/json", "{}");
    vi.advanceTimersByTime(59_999);
    expect(revoked).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(revoked).toEqual([blob(1)]);
    vi.advanceTimersByTime(400);
    expect(revoked).toEqual([blob(1), blob(2)]);
  });

  it("starts at once again after the pause has passed", () => {
    install(false);
    exportFile("a.yaml", "application/yaml", "a");
    vi.advanceTimersByTime(400);
    exportFile("b.yaml", "application/yaml", "b");
    expect(page.clicked.map((c) => c.download)).toEqual(["a.yaml", "b.yaml"]);
  });

  it("reports unconfirmed and releases the file when no download can start", () => {
    install(true);
    page.failClick = true;
    expect(exportFile("orders.yaml", "application/yaml", "x")).toBe(
      "unconfirmed",
    );
    expect(revoked).toEqual([blob(1)]);
    expect(page.body.children).toEqual([]);
    vi.mocked(URL.createObjectURL).mockImplementation(() => {
      throw new Error("no memory");
    });
    expect(exportFile("orders.yaml", "application/yaml", "x")).toBe(
      "unconfirmed",
    );
  });
});

describe("exportNotice", () => {
  it("tells desktop users where the files went", () => {
    install(true);
    expect(isDesktopShell()).toBe(true);
    expect(exportNotice(["orders.yaml"])).toBe(
      "Saved orders.yaml to your Downloads folder.",
    );
    expect(exportNotice(["orders.yaml", "orders.layout.json"])).toBe(
      "Saved orders.yaml and orders.layout.json to your Downloads folder.",
    );
  });

  it("keeps the browser wording and lists three or more files", () => {
    install(false);
    expect(isDesktopShell()).toBe(false);
    expect(exportNotice(["a.yaml", "a.layout.json"])).toBe(
      "Exported a.yaml and a.layout.json.",
    );
    expect(exportNotice(["a.yaml", "b.yaml", "c.yaml"])).toBe(
      "Exported a.yaml, b.yaml, and c.yaml.",
    );
  });
});
