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

"""Public logger filters isolate untrusted driver text to owned broker threads."""

import logging
import threading

_LOGGERS = (
    "aiokafka",
    "aiokafka.helpers",
    "aiokafka.conn",
    "aiokafka.consumer.fetcher",
    "aiokafka.consumer.subscription_state",
    "aiokafka.cluster",
    "aiokafka.consumer.group_coordinator",
    "aiokafka.coordinator.assignors.roundrobin",
    "aiokafka.consumer.consumer",
    "aiokafka.coordinator.assignors.range",
    "aiokafka.coordinator.assignors.abstract",
    "aiokafka.coordinator.assignors.sticky.partition_movements",
    "aiokafka.coordinator.assignors.sticky.sticky_assignor",
    "aiokafka.producer.sender",
    "aiokafka.producer.producer",
    "aiokafka.admin.client",
    "aiokafka.client",
    "asyncio",
)
_lock = threading.Lock()
_owners: set[int] = set()
_installed = False


class BrokerLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        with _lock:
            owned = record.thread in _owners
        if owned:
            record.msg = "BROKER_DRIVER_EVENT"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            record.__dict__.pop("message", None)
        return True


_filter = BrokerLogFilter()


def register() -> None:
    global _installed
    with _lock:
        if not _installed:
            for name in _LOGGERS:
                logging.getLogger(name).addFilter(_filter)
            _installed = True
        _owners.add(threading.get_ident())


def unregister() -> None:
    with _lock:
        _owners.discard(threading.get_ident())
