"""Сборка статического Tailwind CSS лендинга (SPEC.md, §16.1, «SEO и скорость»).

Скачивает standalone-утилиту Tailwind CLI закреплённой версии (без Node и npm),
сверяет SHA-256 и собирает ``web/landing/css/tailwind.css`` (минифицированный)
по классам из ``web/landing/*.html`` и ``script.js``. Собранный CSS хранится в git.

Запуск после правки классов в разметке::

    uv run python tools/landing/build_css.py
"""

from __future__ import annotations

import hashlib
import platform
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

#: Версия Tailwind CLI: ветка v3, совместимая с прежним Play CDN.
VERSION = "3.4.17"

#: SHA-256 бинарников из sha256sums.txt релиза v3.4.17.
SHA256 = {
    "linux-arm64": "69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b",
    "linux-armv7": "704e7d91afba6e1f630889afd0d7db36b4634e628512cc141d504a5beae28860",
    "linux-x64": "7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4",
    "macos-arm64": "a1d0c7985759accca0bf12e51ac1dcbf0f6cf2fffb62e6e0f62d091c477a10a3",
    "macos-x64": "6cbdad74be776c087ffa5e9a057512c54898f9fe8828d3362212dfe32fc933a3",
    "windows-arm64.exe": "76f516476784c00f1562160b5758e3d8f0e6c48957efb26b5b50fbdfd76aa382",
    "windows-x64.exe": "67f1c5e3f5a03406a7bf5badf5ada09b79f3ae78ec43450c15f7e983068da346",
}

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONFIG = HERE / "tailwind.config.js"
INPUT = HERE / "tailwind.input.css"
OUTPUT = ROOT / "web" / "landing" / "css" / "tailwind.css"
#: Кеш бинарника — вне git (каталог .cache в .gitignore).
CACHE = ROOT / ".cache" / "tailwind"


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """Суффикс имени бинарника Tailwind CLI для текущей ОС и архитектуры."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "armv7"}
    if machine not in arch:
        raise SystemExit(f"Неизвестная архитектура: {machine}")
    os_name = {"linux": "linux", "darwin": "macos", "windows": "windows"}.get(system)
    if os_name is None:
        raise SystemExit(f"Неизвестная ОС: {system}")
    key = f"{os_name}-{arch[machine]}" + (".exe" if os_name == "windows" else "")
    if key not in SHA256:
        raise SystemExit(f"Для {key} нет бинарника Tailwind CLI {VERSION}")
    return key


def sha256(path: Path) -> str:
    """SHA-256 файла в hex."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_cli() -> Path:
    """Путь к проверенному бинарнику Tailwind CLI; скачивает при отсутствии."""
    key = platform_key()
    binary = CACHE / f"tailwindcss-{VERSION}-{key}"
    if not binary.exists() or sha256(binary) != SHA256[key]:
        CACHE.mkdir(parents=True, exist_ok=True)
        url = (
            "https://github.com/tailwindlabs/tailwindcss/releases/download/"
            f"v{VERSION}/tailwindcss-{key}"
        )
        print(f"Скачиваю {url}")
        partial = binary.with_suffix(".part")
        urllib.request.urlretrieve(url, partial)
        if sha256(partial) != SHA256[key]:
            partial.unlink()
            raise SystemExit("SHA-256 Tailwind CLI не совпал — файл удалён, сборка остановлена")
        partial.replace(binary)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return binary


def build() -> None:
    """Собрать минифицированный CSS лендинга."""
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(ensure_cli()), "-c", str(CONFIG), "-i", str(INPUT), "-o", str(OUTPUT), "--minify"],
        check=True,
    )
    print(f"Готово: {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size} байт)")


if __name__ == "__main__":
    build()
    sys.exit(0)
