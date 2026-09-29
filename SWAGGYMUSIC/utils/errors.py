#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/KURIGRAMSWAG > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/Yuki77394/KURIGRAMSWAG/blob/master/LICENSE >
# All rights reserved.

import logging
import sys
from functools import wraps

from SWAGGYMUSIC import LOGGER


def capture(func):
    """Decorator that logs and swallows exceptions in async helpers.

    Kept for parity with the original project. Music-specific helpers that
    used it have been removed; nothing in the standalone build relies on
    it, but it is exposed for any future utility code.
    """

    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            LOGGER("SWAGGYMUSIC.errors").warning(
                f"Suppressed error in {func.__name__}: {type(exc).__name__}: {exc}"
            )

    return wrapper


def capture_internal_err(func):
    """Async decorator that returns a graceful user-facing error message
    instead of raising. Reserved for parity with the original project."""
    from SWAGGYMUSIC.utils.exceptions import AssistantErr  # noqa: F401

    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            LOGGER("SWAGGYMUSIC.errors").warning(
                f"Internal error in {func.__name__}: {type(exc).__name__}: {exc}"
            )
            return (
                f"⚠️ <b>Internal error:</b> <code>{type(exc).__name__}: "
                f"{str(exc)[:300]}</code>"
            )

    return wrapper
