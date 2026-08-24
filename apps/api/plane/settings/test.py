# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Test Settings"""

from .common import *  # noqa

DEBUG = True

# Exercise transaction-aware code against a non-default connection while using
# the same physical test database.
DATABASES["soft_delete_test"] = {  # noqa: F405
    **DATABASES["default"],  # noqa: F405
    "TEST": {"MIRROR": "default"},
}

# Send it in a dummy outbox
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

INSTALLED_APPS.append(  # noqa
    "plane.tests"
)
