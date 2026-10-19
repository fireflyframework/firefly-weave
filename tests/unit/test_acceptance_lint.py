# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Studio journeys against the real platform use no request or clock mocking, no forced or synthetic input and no
sign-in shortcut.

The lint reads every TypeScript file under studio/tests/acceptance, at any depth (Playwright loads specs from
subfolders too), and follows their relative imports: in a file outside that folder it reads only the top-level
declarations the journeys use, directly or through other declarations of that file. page.evaluate is allowed only
for read-only audits and measurements; each call says so on the line before it, and no storage write may appear.
Browser contexts and pages are created only by the journey harness, so every person signs in through the real host
and has their browser storage captured.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JOURNEYS = ROOT / "studio/tests/acceptance"
HARNESS = JOURNEYS / "harness.ts"
FORBIDDEN = {
    # Network mocking and shaping
    r"\.route\(": "request interception",
    r"\[\s*[\"'`]route": "request interception",
    r"routeFromHAR": "recorded responses",
    r"routeWebSocket": "socket interception",
    r"\.unroute": "request interception",
    r"\.fulfill\(": "fulfilled request",
    r"\.continue\(\s*\{": "rewritten request",
    r"newCDPSession": "browser protocol session",
    r"setOffline": "network shaping",
    r"\boffline\s*:": "network shaping",
    r"recordHar": "recorded traffic",
    r"serviceWorkers\(": "service worker access",
    # Sign-in shortcuts
    r"storageState": "stored sign-in state",
    r"addCookies": "injected cookies",
    r"extraHTTPHeaders|setExtraHTTPHeaders": "injected headers",
    r"httpCredentials": "injected credentials",
    r"request\.newContext": "API request context",
    r"\.request\.(?:post|put|patch|delete|head|fetch)\(": "API request",
    r"(?:access|id|refresh)_token=": "token in a URL",
    # Forced or synthetic input, clock control and page writes
    r"force\s*:\s*true": "forced action",
    r"dispatchEvent": "synthetic event",
    r"insertText": "synthetic input",
    r"javascript:": "script URL",
    r"\.clock\.": "clock control",
    r"addInitScript": "page script injection",
    r"evaluateHandle": "page handle evaluation",
    r"\$\$?eval\(": "element evaluation",
    r"evaluateAll\(": "element evaluation",
    r"expose(?:Binding|Function)": "page binding",
    r"\b(?:setItem|removeItem)\(": "storage write",
    r"Storage\.clear\(": "storage write",
    r"document\.cookie\s*=": "cookie write",
    r"deleteDatabase": "storage write",
}
# rmSync(path, { recursive: true, force: true }) removes a folder; it is no forced click.
REMOVAL = re.compile(r"\brm(?:Sync)?\(")
# Only in the journeys themselves: the harness's helpers may call the identity provider's admin API from Node.
JOURNEY_ONLY = {r"\bfetch\(": "direct request"}
# Every browser context and page comes from Journey.signIn.
HARNESS_ONLY = {
    r"\.newContext\(": "browser context outside Journey.signIn",
    r"\.newPage\(": "page outside Journey.signIn",
}
DECLARATION = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?(?:function\*?|const|let|var|class|interface|type|enum)"
    r"\s+([A-Za-z_$][\w$]*)"
)
IMPORT = re.compile(r"import\s+(type\s+)?(?:\{([^}]*)\}|([A-Za-z_$][\w$]*))?\s*(?:from\s+)?[\"'](\.{1,2}/[^\"']+)[\"']")


def lint(text: str, *, journey: bool = True, harness: bool = False) -> list[str]:
    """The forbidden forms in ``text``: one finding per match."""
    rules = dict(FORBIDDEN)
    if journey:
        rules |= JOURNEY_ONLY
    if not harness:
        rules |= HARNESS_ONLY
    lines = text.splitlines()
    found = [
        f"{why}: {match.group(0)}"
        for pattern, why in rules.items()
        for match in re.finditer(pattern, text)
        if not (why == "forced action" and REMOVAL.search(line_of(text, match.start())))
    ]
    for number, line in enumerate(lines):
        if ".evaluate(" in line:
            previous = next((earlier for earlier in reversed(lines[:number]) if earlier.strip()), "")
            if "read-only audit" not in previous:
                found.append(f"page.evaluate without a read-only audit note on line {number + 1}")
    return found


def line_of(text: str, offset: int) -> str:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return text[start : end if end >= 0 else len(text)]


def declarations(text: str) -> tuple[str, dict[str, str]]:
    """The file's header (its imports) and each top-level declaration's text, by name."""
    header: list[str] = []
    chunks: dict[str, list[str]] = {}
    current: list[str] = header
    for line in text.splitlines():
        match = DECLARATION.match(line)
        if match:
            current = chunks.setdefault(match.group(1), [])
        current.append(line)
    return "\n".join(header), {name: "\n".join(body) for name, body in chunks.items()}


def imports(path: Path, text: str) -> dict[Path, set[str]]:
    """Relative value imports: the file each names and the local names it binds (``*`` for a default import)."""
    found: dict[Path, set[str]] = {}
    for match in IMPORT.finditer(text):
        if match.group(1):
            continue
        target = (path.parent / match.group(4)).resolve()
        target = target if target.suffix == ".ts" else target.with_name(target.name + ".ts")
        names = {part.split(" as ")[-1].strip() for part in (match.group(2) or "").split(",") if part.strip()}
        names = {name for name in names if not name.startswith("type ")}
        if match.group(3):
            names.add(match.group(3))
        found.setdefault(target, set()).update(names)
    return found


def reached(journeys: Path = JOURNEYS) -> dict[Path, str]:
    """The code Playwright can run for the journeys: every file under ``journeys`` whole, and in each file they
    import, the declarations they use."""
    code: dict[Path, set[str] | None] = {path.resolve(): None for path in journeys.rglob("*.ts")}
    pending = list(code)
    while pending:
        path = pending.pop()
        text = path.read_text(encoding="utf-8")
        header, chunks = declarations(text)
        wanted = code[path]
        names = set(chunks) if wanted is None else set(wanted)
        # Declarations the reached ones use, within this file.
        grow = list(names)
        while grow:
            body = chunks.get(grow.pop(), "")
            for other in chunks:
                if other not in names and re.search(rf"(?<![\w$.]){re.escape(other)}(?![\w$])", body):
                    names.add(other)
                    grow.append(other)
        if wanted is not None:
            code[path] = names
        used = text if wanted is None else "\n".join(chunks.get(name, "") for name in names)
        for target, bound in imports(path, header).items():
            needed = {name for name in bound if re.search(rf"(?<![\w$.]){re.escape(name)}(?![\w$])", used)}
            if not needed or target in code and code[target] is None:
                continue
            before = code.get(target)
            after = (before or set()) | needed
            if before is None and target in code:
                continue
            if after != before:
                code[target] = after
                pending.append(target)
    texts: dict[Path, str] = {}
    for path, names in code.items():
        text = path.read_text(encoding="utf-8")
        if names is None:
            texts[path] = text
        else:
            _, chunks = declarations(text)
            texts[path] = "\n".join(chunks[name] for name in sorted(names) if name in chunks)
    return texts


def lint_journeys(journeys: Path = JOURNEYS) -> dict[str, list[str]]:
    problems = {}
    for path, text in reached(journeys).items():
        inside = path.is_relative_to(journeys.resolve())
        found = lint(
            text, journey=inside and path.name != HARNESS.name, harness=path == (journeys / HARNESS.name).resolve()
        )
        if found:
            problems[path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)] = found
    return problems


def test_studio_journeys_use_no_mocks_forced_input_sign_in_shortcuts_or_writing_evaluations():
    assert list(JOURNEYS.rglob("*.spec.ts")), "studio/tests/acceptance holds the Studio journeys"
    assert lint_journeys() == {}


def test_the_lint_reads_what_the_journeys_import_from_other_folders():
    texts = reached()
    support = (ROOT / "studio/tests/browser/real-platform-support.ts").resolve()
    assert support in texts and "export async function connectAndSignIn" in texts[support]
    # Declarations no journey reaches stay out: the run starter writes through the page on purpose.
    assert "export async function startDemoRun" not in texts[support]


BYPASSES = {
    "socket interception": 'await page.routeWebSocket("**/ws", (ws) => ws.close());',
    "bracket route": 'await page["route"]("**/*", (r) => r.fulfill({ body: "{}" }));',
    "stored sign-in state": 'const c = await browser.newContext({ storageState: "signed-in.json" });',
    "injected cookies": 'await context.addCookies([{ name: "weave_session", value: token, url: host }]);',
    "injected headers": "await page.setExtraHTTPHeaders({ Authorization: `Bearer ${token}` });",
    "API sign-in": "await page.request.post(`${host}/studio/session`, { data: { code } });",
    "API request context": 'const api = await request.newContext({ extraHTTPHeaders: { Authorization: "Bearer x" } });',
    "element evaluation write": 'await page.$eval("input", (e) => (e.value = "x"));',
    "evaluateAll write": 'await page.locator("input").evaluateAll((els) => els.forEach((e) => e.remove()));',
    "a note that does not hold": (
        '// read-only audit: just looking\nawait page.evaluate(() => localStorage.setItem("k", "v"));'
    ),
    "page binding": 'await page.exposeBinding("fakeAuth", () => "token");',
    "service worker": "await context.serviceWorkers()[0].evaluate(() => 1);",
    "token in a URL": "await page.goto(`${host}/#access_token=${token}`);",
    "script URL": 'await page.goto("javascript:localStorage.setItem(1,2)");',
    "route on the next line": 'await page\n  .route("**/*", fulfil);',
    "force through a variable": "const opts = { force: true }; await b.click(opts);",
    "force with a space": "await b.click({ force : true });",
    "fulfill on its own": "await route.fulfill({ status: 200 });",
    "recorded traffic": 'await browser.newContext({ recordHar: { path: "x" } });',
    "offline context": "await browser.newContext({ offline: true });",
    "synthetic text": 'await page.keyboard.insertText("abc");',
    "browser protocol": (
        "const cdp = await context.newCDPSession(page);"
        ' await cdp.send("Network.setRequestInterception", {patterns: []});'
    ),
    "init script": "await page.addInitScript(() => {});",
    "rewritten request": 'await route.continue({ headers: { cookie: "x" } });',
    "own context": "const context = await browser.newContext();",
    "own page": "const page = await context.newPage();",
    "fetch from a journey": 'await fetch(`${host}/studio/session`, { method: "POST" });',
}


def test_the_lint_catches_each_bypass():
    missed = [name for name, snippet in BYPASSES.items() if not lint(snippet)]
    assert missed == []


def test_the_lint_keeps_ordinary_journey_code():
    ordinary = """
    const page = await j.signIn("builder");
    await openSidebar(page, "Workflows");
    await page.getByRole("button", { name: "Publish…", exact: true }).click();
    await page.keyboard.press("Escape");
    await expect(page.locator(".platform-indicator")).toContainText("Signed in as builder");
    // read-only audit: the font and color scheme the page computed
    const computed = await page.evaluate(() => getComputedStyle(document.body).fontFamily);
    await j.shot(page, "home");
    """
    assert lint(ordinary) == []
    assert lint("const context = await this.browser.newContext({ viewport });", journey=False, harness=True) == []
    assert lint("await page.request.get(`${host.origin}/studio/session`)", journey=False) == []
    assert lint("rmSync(home, { recursive: true, force: true });", journey=False) == []
    assert lint("await button.click({ force: true }); // rmSync elsewhere") != []


def test_the_lint_reads_nested_specs_and_their_imports(tmp_path):
    journeys = tmp_path / "acceptance"
    (journeys / "nested").mkdir(parents=True)
    browser = tmp_path / "browser"
    browser.mkdir()
    (journeys / "harness.ts").write_text("export const ok = 1;\n", encoding="utf-8")
    (journeys / "nested/probe.spec.ts").write_text(
        'import { helper } from "../../browser/help";\nawait helper();\n', encoding="utf-8"
    )
    (browser / "help.ts").write_text(
        "export async function helper() {\n  await inner();\n}\n"
        "async function inner() {\n  await page.addCookies([]);\n}\n"
        "export function unused() {\n  page.route('x');\n}\n",
        encoding="utf-8",
    )
    problems = lint_journeys(journeys)
    assert list(problems.values()) == [["injected cookies: addCookies"]]
