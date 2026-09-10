from __future__ import annotations

import re
from pathlib import Path


# Indicadores que NO forman parte del nombre del modelo.
_VARIANT_SUFFIX = re.compile(
    r"(?i)\s*[-_. ]+(?:unsupported|presupported|lychee|chitubox|"
    r"pre[-_. ]?supported|supports?)\s*$"
)
_PART_PREFIX = re.compile(r"(?i)^\s*p\d+\s+")
_RENDER_PREFIX = re.compile(r"(?i)^\s*render\s+")
_DATE_PREFIX = re.compile(r"^\s*(?:19|20)\d{2}[-_.]\d{1,2}\s*[-_. ]*\s*")
_RAR_PART_SUFFIX = re.compile(r"(?i)[. _-]+part\d+\s*$")


def _strip_extension(filename: str) -> str:
    return re.sub(
        r"(?i)\.(?:zip|rar|7z|stl|obj|fbx|blend|3mf|step|stp|iges|igs|lys|chitubox|ctb|phz|photon)$",
        "",
        Path(filename).name,
    )


def extract_3d_name(filename: str) -> str:
    """
    Extrae un nombre de carpeta razonable para modelos 3D.

    Ejemplos:
      RifleLadsB-Unsupported.zip -> RifleLadsB
      RifleLadsB-Presupported.zip -> RifleLadsB
      RifleLadsB-Lychee.zip -> RifleLadsB
      Gear Guts - Mek Baby O.rar -> Gear Guts - Mek Baby
      Gear Guts - Mek Baby N.rar -> Gear Guts - Mek Baby
      p1 2026-03 Legend of the Vikings - Across the Realms.7z
          -> Legend of the Vikings - Across the Realms
      Render 2026-03 Legend of the Vikings - Across the Realms.7z
          -> Legend of the Vikings - Across the Realms
      2025-04 - #124 Deep Sea Mysteries.part1.rar
          -> #124 Deep Sea Mysteries
    """
    name = _strip_extension(filename)

    # Partes de archivos RAR: .part1, .part2, ...
    name = _RAR_PART_SUFFIX.sub("", name)

    # Prefijos habituales de publicaciones.
    name = _PART_PREFIX.sub("", name)
    name = _RENDER_PREFIX.sub("", name)

    # Fechas tipo 2026-03 / 2025-04.
    name = _DATE_PREFIX.sub("", name)

    # Variantes de impresión/software que comparten el mismo modelo.
    name = _VARIANT_SUFFIX.sub("", name)

    # Volúmenes nombrados con una única letra: "Mek Baby O.rar".
    # Solo se elimina cuando la letra va separada por espacio/separador.
    name = re.sub(r"(?i)(?:\s+|[-_.])([A-Z])$", "", name)

    # Separadores habituales.
    name = name.replace("_", " ")
    name = re.sub(r"[.]+", " ", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" -_. ,")

    # No crear carpetas con nombres vacíos.
    return name or "Modelo 3D"
