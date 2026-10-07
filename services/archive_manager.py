from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from config import AUTO_EXTRACT, DELETE_ARCHIVES_AFTER_EXTRACT


@dataclass
class ArchiveGroup:
    key: str
    first: Path
    parts: list[Path]


@dataclass
class ExtractionReport:
    extracted_groups: int
    deleted_archives: int
    errors: list[str]


_PATTERNS = [
    (re.compile(r"(?i)^(.*)\.part(\d+)\.rar$"), "part-rar"),
    (re.compile(r"(?i)^(.*)\.rar$"), "rar"),
    (re.compile(r"(?i)^(.*)\.r(\d{2})$"), "rar-old"),
    (re.compile(r"(?i)^(.*)\.7z\.(\d{3})$"), "7z-split"),
    (re.compile(r"(?i)^(.*)\.zip\.(\d{3})$"), "zip-split"),
    (re.compile(r"(?i)^(.*)\.z(\d{2})$"), "zip-old"),
    (re.compile(r"(?i)^(.*)\.zip$"), "zip"),
    (re.compile(r"(?i)^(.*)\.7z$"), "7z"),
]


def _classify(path: Path):
    name = path.name
    for regex, kind in _PATTERNS:
        match = regex.match(name)
        if match:
            base = match.group(1).lower()
            number = int(match.group(2)) if match.lastindex and match.lastindex >= 2 else None
            return kind, base, number
    return None


def _discover_groups(directory: Path) -> list[ArchiveGroup]:
    files = [p for p in directory.iterdir() if p.is_file()]
    classified = [(p, _classify(p)) for p in files]
    groups: list[ArchiveGroup] = []
    used: set[Path] = set()

    # partNN.rar
    bases = {info[1] for _, info in classified if info and info[0] == "part-rar"}
    for base in bases:
        parts = sorted(
            [p for p, info in classified if info and info[0] == "part-rar" and info[1] == base],
            key=lambda p: _classify(p)[2],
        )
        nums = [_classify(p)[2] for p in parts]
        if not nums or nums[0] != 1 or nums != list(range(1, nums[-1] + 1)):
            continue
        groups.append(ArchiveGroup(f"part-rar:{base}", parts[0], parts))
        used.update(parts)

    # 7z.001 / zip.001
    for kind in ("7z-split", "zip-split"):
        bases = {info[1] for _, info in classified if info and info[0] == kind}
        for base in bases:
            parts = sorted(
                [p for p, info in classified if info and info[0] == kind and info[1] == base],
                key=lambda p: _classify(p)[2],
            )
            nums = [_classify(p)[2] for p in parts]
            if not nums or nums[0] != 1 or nums != list(range(1, nums[-1] + 1)):
                continue
            groups.append(ArchiveGroup(f"{kind}:{base}", parts[0], parts))
            used.update(parts)

    # RAR antiguo: .rar + .r00 + .r01...
    for rar, info in classified:
        if not info or info[0] != "rar" or rar in used:
            continue
        base = info[1]
        old_parts = sorted(
            [p for p, i in classified if i and i[0] == "rar-old" and i[1] == base],
            key=lambda p: _classify(p)[2],
        )
        if old_parts:
            nums = [_classify(p)[2] for p in old_parts]
            if nums != list(range(0, nums[-1] + 1)):
                continue
            parts = [rar, *old_parts]
            groups.append(ArchiveGroup(f"rar-old:{base}", rar, parts))
            used.update(parts)
        else:
            groups.append(ArchiveGroup(f"rar:{base}", rar, [rar]))
            used.add(rar)

    # ZIP antiguo: .z01 ... + .zip
    for zip_file, info in classified:
        if not info or info[0] != "zip" or zip_file in used:
            continue
        base = info[1]
        zparts = sorted(
            [p for p, i in classified if i and i[0] == "zip-old" and i[1] == base],
            key=lambda p: _classify(p)[2],
        )
        if zparts:
            nums = [_classify(p)[2] for p in zparts]
            if nums != list(range(1, nums[-1] + 1)):
                continue
            parts = [*zparts, zip_file]
            groups.append(ArchiveGroup(f"zip-old:{base}", zip_file, parts))
            used.update(parts)
        else:
            groups.append(ArchiveGroup(f"zip:{base}", zip_file, [zip_file]))
            used.add(zip_file)

    # 7z simple
    for p, info in classified:
        if info and info[0] == "7z" and p not in used:
            groups.append(ArchiveGroup(f"7z:{info[1]}", p, [p]))
            used.add(p)

    return groups


async def _run_7z(*args: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "7z", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode, output.decode("utf-8", errors="replace")


async def _paths_are_safe(archive: Path) -> tuple[bool, str]:
    code, output = await _run_7z("l", "-slt", str(archive))
    if code != 0:
        return False, output[-1000:]
    for line in output.splitlines():
        if not line.startswith("Path = "):
            continue
        value = line[7:].replace("\\", "/")
        p = PurePosixPath(value)
        if p.is_absolute() or ".." in p.parts:
            return False, f"Ruta insegura dentro del comprimido: {value}"
    return True, ""


async def extract_archives(directory: Path) -> ExtractionReport:
    if not AUTO_EXTRACT:
        return ExtractionReport(0, 0, [])
    if shutil.which("7z") is None:
        return ExtractionReport(0, 0, ["7z no está instalado en el contenedor."])

    extracted = 0
    deleted = 0
    errors: list[str] = []

    for group in _discover_groups(directory):
        safe, detail = await _paths_are_safe(group.first)
        if not safe:
            errors.append(f"{group.first.name}: no se extrae ({detail.strip()})")
            continue

        # Primero prueba todas las partes/CRC. Si falta una parte, 7z falla aquí.
        code, output = await _run_7z("t", "-y", str(group.first))
        if code != 0:
            errors.append(f"{group.first.name}: prueba del archivo fallida; se conservan los comprimidos. {output[-500:].strip()}")
            continue

        code, output = await _run_7z("x", "-y", f"-o{directory}", str(group.first))
        if code != 0:
            errors.append(f"{group.first.name}: extracción fallida; se conservan los comprimidos. {output[-500:].strip()}")
            continue

        extracted += 1
        if DELETE_ARCHIVES_AFTER_EXTRACT:
            for part in group.parts:
                try:
                    part.unlink()
                    deleted += 1
                except OSError as error:
                    errors.append(f"{part.name}: extraído correctamente, pero no se pudo borrar: {error}")

    return ExtractionReport(extracted, deleted, errors)
