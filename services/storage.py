from pathlib import Path
import shutil

from config import MOVIES_PATH, SERIES_PATH, TEMP_PATH, THREED_PATH
from services.series_organizer import extract_series_name
from services.three_d_organizer import extract_3d_name

DESTINATIONS = {"movies": Path(MOVIES_PATH), "series": Path(SERIES_PATH), "3d": Path(THREED_PATH)}
TEMP_DIR = Path(TEMP_PATH)


def get_destination(mode: str) -> Path:
    if mode not in DESTINATIONS:
        raise ValueError(f"Modo desconocido: {mode}")
    return DESTINATIONS[mode]


def prepare_directories():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    for destination in DESTINATIONS.values():
        destination.mkdir(parents=True, exist_ok=True)


def get_temp_batch_dir(user_id: int) -> Path:
    path = TEMP_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_temp_batch(user_id: int):
    path = TEMP_DIR / str(user_id)
    if path.exists():
        shutil.rmtree(path)


def get_unique_filename(directory: Path, filename: str) -> Path:
    original = Path(filename)
    candidate = directory / original.name
    if not candidate.exists():
        return candidate
    stem, suffix, counter = original.stem, original.suffix, 1
    while True:
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _get_file_destination(destination_dir: Path, mode: str, filename: str) -> Path:
    if mode == "series":
        return destination_dir / extract_series_name(filename)
    if mode == "3d":
        return destination_dir / extract_3d_name(filename)
    return destination_dir


def move_batch_to_destination(user_id: int, mode: str) -> tuple[int, list[str]]:
    source_dir = get_temp_batch_dir(user_id)
    destination_dir = get_destination(mode)
    destination_dir.mkdir(parents=True, exist_ok=True)
    entries = list(source_dir.iterdir())
    moved, errors = 0, []

    for source in entries:
        try:
            # Las carpetas creadas por un comprimido se conservan como unidad.
            # Los archivos sueltos mantienen la organización Series/3D existente.
            target_dir = destination_dir if source.is_dir() else _get_file_destination(destination_dir, mode, source.name)
            target_dir.mkdir(parents=True, exist_ok=True)
            destination = get_unique_filename(target_dir, source.name)
            shutil.move(str(source), str(destination))
            moved += 1
        except Exception as error:
            errors.append(f"{source.name}: {error}")

    try:
        source_dir.rmdir()
    except OSError:
        pass
    return moved, errors
