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

"""Terminal plumbing for `weave auth`: prompts and guidance on stderr, results on stdout.

Text mode prints a human summary on stdout; `--output json` prints exactly one
JSON document on stdout and nothing else. Prompts only appear when stdin and
stderr are terminals and the output is text, so scripts never block on input:
a missing answer is a `WV-AUTH-INPUT` failure that names the flag to pass.
Failures are stable codes with plain-language messages and never tracebacks.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NoReturn

import click

EXIT_OK = 0
EXIT_AUTH = 1
EXIT_INPUT = 2
EXIT_REMOTE = 3


class AuthCommandError(Exception):
    """A command failure: stable code, plain message, exit code, extra JSON keys and guidance lines."""

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int,
        *,
        details: Mapping[str, Any] | None = None,
        lines: Sequence[str] = (),
    ) -> None:
        self.code, self.message, self.exit_code = code, message, exit_code
        self.details = dict(details or {})
        self.lines = tuple(lines)
        super().__init__(code)


def input_required(flag: str, message: str | None = None) -> AuthCommandError:
    """`WV-AUTH-INPUT` naming the flag (or argument) that supplies the missing answer."""
    return AuthCommandError(
        "WV-AUTH-INPUT",
        message or f"Missing input: pass {flag}. Questions are only asked in an interactive terminal.",
        EXIT_INPUT,
        details={"flag": flag},
    )


def terminal_is_interactive() -> bool:
    """Prompts need a person: both stdin and stderr must be terminals."""
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stderr and sys.stderr.isatty())
    except (AttributeError, ValueError):
        return False


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class Console:
    """Prompts and guidance on stderr; the only writer of the command result on stdout.

    `read` and `read_confirm` are the interactivity seam: tests replace them to
    feed answers or to raise `KeyboardInterrupt`/`click.Abort` at any question.
    """

    def __init__(self, output: str, *, interactive: bool | None = None) -> None:
        self.output = output
        self.json = output == "json"
        detected = terminal_is_interactive() if interactive is None else interactive
        self.interactive = detected and not self.json

    # --- guidance (stderr) -------------------------------------------------------------------------------------

    def say(self, text: str = "") -> None:
        click.echo(text, err=True)

    def step(self, number: int, title: str) -> None:
        self.say()
        self.say(click.style(f"Step {number} of 4 · {title}", bold=True))

    # --- questions (stderr, interactive only) ------------------------------------------------------------------

    def read(self, label: str, default: str | None) -> str:
        value = click.prompt(label, default=default, err=True, show_default=default is not None)
        return str(value)

    def read_confirm(self, label: str, default: bool) -> bool:
        return bool(click.confirm(label, default=default, err=True))

    def ask(self, label: str, *, flag: str, default: str | None = None) -> str:
        if not self.interactive:
            raise input_required(flag)
        while True:
            value = self.read(label, default).strip()
            if value:
                return value
            if default is not None:
                return default

    def confirm(self, label: str, *, flag: str, default: bool = False) -> bool:
        if not self.interactive:
            raise input_required(flag)
        return self.read_confirm(label, default)

    def choose(self, label: str, options: Sequence[str], *, flag: str) -> int:
        """Numbered choice; returns the zero-based index."""
        if not self.interactive:
            raise input_required(flag)
        for index, option in enumerate(options, 1):
            self.say(f"  {index}. {option}")
        while True:
            answer = self.read(label, "1" if len(options) == 1 else None).strip()
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            self.say(f"Enter a number from 1 to {len(options)}.")

    # --- results (stdout) --------------------------------------------------------------------------------------

    def result(self, payload: Any, text: Sequence[str]) -> None:
        if self.json:
            click.echo(dumps(payload))
            return
        for line in text:
            click.echo(line)

    def failure(self, error: AuthCommandError) -> NoReturn:
        self.say(error.message)
        for line in error.lines:
            self.say(line)
        if error.code != "WV-AUTH-CANCELLED":
            self.say(f"Support code: {error.code}")
        if self.json:
            click.echo(dumps({**error.details, "code": error.code, "message": error.message}))
        raise click.exceptions.Exit(error.exit_code)


def console_for(output: str) -> Console:
    """Factory used by every command; tests replace it to inject a scripted console."""
    return Console(output)


def cancelled(message: str = "Cancelled.", **details: Any) -> AuthCommandError:
    return AuthCommandError("WV-AUTH-CANCELLED", message, EXIT_AUTH, details=details)


def run(
    console: Console,
    action: Callable[[], None],
    *,
    on_cancel: Callable[[], AuthCommandError] | None = None,
    on_failure: Callable[[AuthCommandError], AuthCommandError] | None = None,
    profile: str | Callable[[], str | None] | None = None,
) -> None:
    """Run a command body; map failures and Ctrl+C to stable codes, exit codes and no tracebacks."""
    from firefly_weave.cli.auth_flow import translate

    try:
        action()
        return
    except AuthCommandError as error:
        failure = error
    except (KeyboardInterrupt, click.Abort, EOFError, asyncio.CancelledError):
        if console.interactive:
            # The interrupted prompt left the cursor on its line.
            console.say()
        console.failure(on_cancel() if on_cancel is not None else cancelled())
    except Exception as error:
        mapped = translate(error, profile=profile() if callable(profile) else profile)
        if mapped is None:
            raise
        failure = mapped
    console.failure(on_failure(failure) if on_failure is not None else failure)
