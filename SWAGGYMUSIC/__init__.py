#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
#
# This file is part of < https://github.com/Yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/Yuki77394/SWAGGYHELPER/blob/main/LICENSE >
#
# All rights reserved.
#
# SWAGGYMUSIC — standalone channel-management build.
# Music, voice-stream and assistant-client code has been removed; the
# package now keeps the original architecture, Start UI and /createpost
# wizard, plus three channel-management features:
#     /setdelay   /setgap   /createpost

from SWAGGYMUSIC.core.bot import Alone
from SWAGGYMUSIC.core.dir import dirr
from SWAGGYMUSIC.misc import dbb, heroku

from .logging import LOGGER

dirr()
dbb()
heroku()

app = Alone()

from .logging import LOGGER  # noqa: F401  (re-export for plugins)
