#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/Yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.

import logging


def get_logger(name: str) -> logging.Logger:
    """Reserved logger factory kept for parity with the original project."""
    from SWAGGYMUSIC.logging import LOGGER

    return LOGGER(name)
