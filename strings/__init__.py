#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/KURIGRAMSWAG > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/Yuki77394/KURIGRAMSWAG/blob/master/LICENSE >
# All rights reserved.

import os
from typing import List

import yaml

# Resolve the langs/ directory relative to this file so the strings package
# works regardless of CWD (e.g. when installed via `pip install -e .` or run
# via the `swaggymusic` console script, not just `python -m SWAGGYMUSIC`
# from the project root).
_LANGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "langs")

languages = {}
languages_present = {}


def get_string(lang: str):
    return languages[lang]


for filename in os.listdir(_LANGS_DIR):
    if "en" not in languages:
        languages["en"] = yaml.safe_load(
            open(os.path.join(_LANGS_DIR, "en.yml"), encoding="utf8")
        )
        languages_present["en"] = languages["en"]["name"]
    if filename.endswith(".yml"):
        language_name = filename[:-4]
        if language_name == "en":
            continue
        languages[language_name] = yaml.safe_load(
            open(os.path.join(_LANGS_DIR, filename), encoding="utf8")
        )
        for item in languages["en"]:
            if item not in languages[language_name]:
                languages[language_name][item] = languages["en"][item]
    try:
        languages_present[language_name] = languages[language_name]["name"]
    except Exception:
        print("There is some issue with the language file inside bot.")
        exit()
