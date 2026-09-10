from __future__ import annotations

import re
from pathlib import Path


SEASON_EPISODE_PATTERNS = [
    re.compile(r"(?i)(?:^|[\s._-])s\d{1,2}e\d{1,3}(?=$|[\s._,\-[({])"),
    re.compile(r"(?i)(?:^|[\s._-])t\d{1,2}e\d{1,3}(?=$|[\s._,\-[({])"),
    re.compile(r"(?i)(?:^|[\s._-])t\d{1,2}\s*,?\s*e\d{1,3}(?=$|[\s._,\-[({])"),
    re.compile(r"(?i)(?:^|[\s._-])\d{1,2}x\d{1,3}(?=$|[\s._,\-[({])"),
    re.compile(r"(?i)(?:^|[\s._-])(?:temporada|temp|season)\s*\.?\s*\d{1,2}(?=$|[\s._,\-[({])"),
]

TECHNICAL = re.compile(
    r"(?i)(?<![a-z0-9])(?:1080p|2160p|1440p|720p|480p|4k|8k|web[-_. ]?dl|"
    r"web|hdtv|bluray|bdrip|brrip|dvdrip|hdr10\+?|hdr|dolby|vision|hevc|"
    r"h\.?265|h\.?264|x265|x264|aac|ac3|dts|multi|dual|castellano|"
    r"español|latino)(?![a-z0-9])"
)


def extract_series_name(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"(?i)\.(?:mkv|mp4|avi|mov|wmv|m4v|ts|rar|zip|7z)$", "", name)
    name = re.sub(r"(?i)(?:^|[\s._-])(?:part|pt)\s*[-_. ]?\s*\d+\b", " ", name)

    # Eliminar años y bloques técnicos entre []/().
    name = re.sub(r"(?i)\s*[\[(]\s*(?:19|20)\d{2}\s*[\])]", " ", name)
    name = re.sub(
        r"(?i)\s*[\[(][^()\[\]]*(?:1080p|2160p|720p|WEB[-_. ]?DL|WEB|HDTV|BluRay|HDR|HEVC|H\.?265|H\.?264)[^()\[\]]*[\])]",
        " ", name,
    )

    marker_start = None
    for pattern in SEASON_EPISODE_PATTERNS:
        match = pattern.search(name)
        if match and (marker_start is None or match.start() < marker_start):
            marker_start = match.start()

    title = name[:marker_start] if marker_start is not None else name

    # Algunos releases incluyen un número de colección/temporada antes del
    # marcador, por ejemplo: Star_Wars_Visions_Presents_6_1x01.
    # Ese número no forma parte del nombre de la serie.
    if marker_start is not None:
        title = re.sub(r"[\s._-]+\d{1,2}\s*$", "", title)

    title = re.sub(r"(?i)(?:^|[\s._-])(?:19|20)\d{2}(?=$|[\s._,-])", " ", title)
    title = TECHNICAL.sub(" ", title)
    title = title.replace("_", " ")
    title = re.sub(r"[.]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -_. ,")

    return title or "Serie"
