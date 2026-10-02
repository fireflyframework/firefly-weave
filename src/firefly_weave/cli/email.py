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
"""Thin authenticated SDK commands for email; no transport retries."""

import click

from firefly_weave.cli.remote import family

email = click.Group("email", help="Scoped conversations and fenced submissions.")
email.add_command(family("conversations", "email_conversations"))
email.add_command(family("submissions", "email_submissions"))

email.add_command(family("sources", "email_sources"))
email.add_command(family("receipts", "email_receipts"))
email.add_command(family("tokens", "email_tokens"))
