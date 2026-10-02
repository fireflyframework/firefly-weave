# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Deadline-owned asynchronous mail sockets with DNS pinning and no transport retries."""

import asyncio
import base64
import ipaddress
import ssl
from dataclasses import dataclass

from pyfly.container import service

from firefly_weave.connectors.egress import EgressDenied, EgressPolicy, resolve
from firefly_weave.contracts.email import MailProfile


@dataclass(frozen=True)
class MailPolicy:
    private_networks: tuple[str, ...] = ()
    allowed_ports: tuple[int, ...] = (25, 465, 587, 143, 993)
    allow_local_fixture: bool = False

    def validate(self, profile: MailProfile) -> None:
        if profile.port not in self.allowed_ports or not set(profile.private_networks).issubset(self.private_networks):
            raise EgressDenied()
        if profile.tls == "local_fixture" and not self.allow_local_fixture:
            raise EgressDenied()


DEFAULT_MAIL_POLICY = MailPolicy()


@dataclass(frozen=True)
class SMTPResult:
    state: str
    accepted_recipients: tuple[str, ...] = ()
    rejected_recipients: tuple[str, ...] = ()


async def open_mail(profile: MailProfile, policy: MailPolicy) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    policy.validate(profile)
    addresses = await resolve(profile.host, profile.port)
    url = f"https://{profile.host}:{profile.port}"
    EgressPolicy((url,), policy.private_networks).validate(url, addresses)
    if profile.tls == "local_fixture" and (
        profile.auth != "none" or not all(ipaddress.ip_address(v).is_loopback for v in addresses)
    ):
        raise EgressDenied()
    context = ssl.create_default_context() if profile.tls == "tls" else None
    reader, writer = await asyncio.open_connection(
        addresses[0], profile.port, ssl=context, server_hostname=profile.host if context else None, limit=65536
    )
    peer = writer.get_extra_info("peername")
    if not peer or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(addresses[0]):
        writer.close()
        raise EgressDenied()
    return reader, writer


async def close_mail(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        async with asyncio.timeout(1):
            await writer.wait_closed()
    except (OSError, TimeoutError):
        pass


@service
class SMTPTransport:
    def __init__(self, policy: MailPolicy = DEFAULT_MAIL_POLICY) -> None:
        self.policy = policy

    async def send(
        self, profile: MailProfile, recipients: tuple[str, ...], raw: bytes, credentials: dict[str, str]
    ) -> SMTPResult:
        if (
            not recipients
            or len(recipients) > 50
            or len(raw) > 4 * 1048576
            or not set(recipients).issubset(profile.allowed_recipients)
        ):
            raise ValueError("Message or recipient policy denied")
        writer = None
        accepted: list[str] = []
        rejected: list[str] = []
        data_started = False

        async def response(reader: asyncio.StreamReader) -> int:
            total, code = 0, None
            for _ in range(100):
                line = await reader.readline()
                total += len(line)
                if len(line) < 5 or total > 16384 or not line[:3].isdigit() or line[3:4] not in (b" ", b"-"):
                    raise ValueError("Malformed SMTP response")
                current = int(line[:3])
                if code is not None and code != current:
                    raise ValueError("Malformed SMTP response")
                code = current
                if line[3:4] == b" ":
                    return code
            raise ValueError("SMTP response limit exceeded")

        async def command(reader: asyncio.StreamReader, value: bytes) -> int:
            assert writer is not None
            writer.write(value + b"\r\n")
            await writer.drain()
            return await response(reader)

        try:
            async with asyncio.timeout(profile.timeout_seconds):
                reader, writer = await open_mail(profile, self.policy)
                if await response(reader) != 220 or await command(reader, b"EHLO weave.local") != 250:
                    return SMTPResult("rejected", (), recipients)
                if profile.tls == "starttls":
                    if await command(reader, b"STARTTLS") != 220:
                        return SMTPResult("rejected", (), recipients)
                    await writer.start_tls(ssl.create_default_context(), server_hostname=profile.host)
                    if await command(reader, b"EHLO weave.local") != 250:
                        return SMTPResult("rejected", (), recipients)
                if profile.auth != "none":
                    username = credentials["username"]
                    if any(c in username for c in ("\r", "\n", "\x00", "\x01")):
                        raise ValueError("Invalid authentication identity")
                    if profile.auth == "password":
                        token = base64.b64encode(("\0" + username + "\0" + credentials["password"]).encode())
                        auth = b"AUTH PLAIN " + token
                    else:
                        token = base64.b64encode(
                            ("user=" + username + "\x01auth=Bearer " + credentials["token"] + "\x01\x01").encode()
                        )
                        auth = b"AUTH XOAUTH2 " + token
                    if await command(reader, auth) != 235:
                        return SMTPResult("rejected", (), recipients)
                if await command(reader, f"MAIL FROM:<{profile.sender}>".encode()) != 250:
                    return SMTPResult("rejected", (), recipients)
                for recipient in recipients:
                    code = await command(reader, f"RCPT TO:<{recipient}>".encode())
                    (accepted if code in (250, 251) else rejected).append(recipient)
                if not accepted:
                    return SMTPResult("rejected", (), tuple(rejected))
                if await command(reader, b"DATA") != 354:
                    return SMTPResult("rejected", (), recipients)
                # From this write onward, a disconnect cannot establish non-acceptance.
                data_started = True
                escaped = b"\r\n".join(b"." + line if line.startswith(b".") else line for line in raw.split(b"\r\n"))
                writer.write(escaped.rstrip(b"\r\n") + b"\r\n.\r\n")
                await writer.drain()
                code = await response(reader)
                return SMTPResult(
                    "accepted" if code == 250 else "rejected",
                    tuple(accepted) if code == 250 else (),
                    tuple(rejected) if code == 250 else recipients,
                )
        except (OSError, TimeoutError, ValueError, KeyError):
            return SMTPResult(
                "unknown" if data_started else "rejected",
                tuple(accepted) if data_started else (),
                tuple(rejected) if data_started else recipients,
            )
        finally:
            if writer is not None:
                await close_mail(writer)


@dataclass(frozen=True)
class IMAPBatch:
    uidvalidity: int
    highest_uid: int
    messages: tuple[tuple[int, bytes | None], ...]


@service
class IMAPTransport:
    def __init__(self, policy: MailPolicy = DEFAULT_MAIL_POLICY) -> None:
        self.policy = policy

    async def poll(
        self,
        profile: MailProfile,
        credentials: dict[str, str],
        after_uid: int | None,
        *,
        limit: int = 20,
        max_bytes: int = 4 * 1048576,
    ) -> IMAPBatch:
        writer = None
        tag = 0

        async def command(reader: asyncio.StreamReader, value: bytes) -> list[bytes]:
            nonlocal tag
            assert writer is not None
            tag += 1
            prefix = f"A{tag}".encode()
            writer.write(prefix + b" " + value + b"\r\n")
            await writer.drain()
            lines, total = [], 0
            while True:
                line = await reader.readline()
                if not line or len(line) > 16384:
                    raise ValueError("Malformed IMAP response")
                total += len(line)
                if total > 65536:
                    raise ValueError("IMAP response limit")
                if line.startswith(prefix + b" "):
                    if not line.startswith(prefix + b" OK"):
                        raise ValueError("IMAP command rejected")
                    return lines
                lines.append(line)

        def quoted(value: str) -> bytes:
            if any(ord(c) < 32 for c in value):
                raise ValueError("Invalid IMAP argument")
            return ('"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"').encode()

        try:
            async with asyncio.timeout(profile.timeout_seconds):
                reader, writer = await open_mail(profile, self.policy)
                if not (await reader.readline()).startswith(b"* OK"):
                    raise ValueError("IMAP greeting rejected")
                if profile.tls == "starttls":
                    await command(reader, b"STARTTLS")
                    await writer.start_tls(ssl.create_default_context(), server_hostname=profile.host)
                if profile.auth == "password":
                    await command(
                        reader, b"LOGIN " + quoted(credentials["username"]) + b" " + quoted(credentials["password"])
                    )
                elif profile.auth == "oauth":
                    token = base64.b64encode(
                        (
                            "user=" + credentials["username"] + "\x01auth=Bearer " + credentials["token"] + "\x01\x01"
                        ).encode()
                    )
                    await command(reader, b"AUTHENTICATE XOAUTH2 " + token)
                lines = await command(reader, b"EXAMINE " + quoted(profile.folder))
                import re

                validity = next((int(m[1]) for line in lines if (m := re.search(rb"UIDVALIDITY (\d+)", line))), None)
                if validity is None or validity <= 0:
                    raise ValueError("Missing or invalid UIDVALIDITY")
                lines = await command(reader, b"UID SEARCH ALL")
                uids = [int(v) for line in lines if line.startswith(b"* SEARCH ") for v in line.split()[2:]]
                if any(v <= 0 for v in uids):
                    raise ValueError("Invalid IMAP UID")
                highest = max(uids, default=0)
                if after_uid is None:
                    return IMAPBatch(validity, highest, ())
                messages: list[tuple[int, bytes | None]] = []
                for uid in sorted(v for v in uids if v > after_uid)[: min(limit, 100)]:
                    # Size first; never allocate or fetch an oversized literal.
                    sizes = await command(reader, f"UID FETCH {uid} (RFC822.SIZE)".encode())
                    size = next((int(m[1]) for line in sizes if (m := re.search(rb"RFC822.SIZE (\d+)", line))), None)
                    if size is None:
                        raise ValueError("Missing message size")
                    if size > max_bytes:
                        messages.append((uid, None))
                        continue
                    tag += 1
                    prefix = f"A{tag}".encode()
                    writer.write(prefix + f" UID FETCH {uid} (BODY.PEEK[])\r\n".encode())
                    await writer.drain()
                    literal = None
                    total = 0
                    while True:
                        line = await reader.readline()
                        total += len(line)
                        if not line or total > 65536:
                            raise ValueError("Malformed IMAP fetch")
                        match = re.search(rb"\{(\d+)\}\r\n$", line)
                        if match:
                            amount = int(match[1])
                            if amount > max_bytes or literal is not None:
                                raise ValueError("IMAP literal limit")
                            literal = await reader.readexactly(amount)
                        if line.startswith(prefix + b" "):
                            if not line.startswith(prefix + b" OK") or literal is None:
                                raise ValueError("IMAP fetch rejected")
                            break
                    messages.append((uid, literal))
                return IMAPBatch(validity, highest, tuple(messages))
        finally:
            if writer is not None:
                await close_mail(writer)
