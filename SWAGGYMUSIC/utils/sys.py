#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/KURIGRAMSWAG > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/Yuki77394/KURIGRAMSWAG/blob/master/LICENSE >
# All rights reserved.

import logging


def get_file_size(file_path: str) -> int:
    """Return a file's size in bytes. Kept for parity with the original
    project; nothing in the standalone build currently uses it but a
    future utility plugin might."""
    import os

    try:
        return os.path.getsize(file_path)
    except OSError:
        return 0
