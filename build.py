"""Build a Windows onedir DropGain release folder.

Prerequisites:
  pip install -r requirements.txt -r requirements-build.txt
  Place an LGPL FFmpeg essentials build in third_party/ffmpeg/ (see README.DropGain.txt).

Output:
  release/DropGain/          runnable folder
  release/DropGain-win64.zip optional archive (--zip)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "DropGain"
RELEASE = ROOT / "release" / "DropGain"
RELEASE_ZIP = ROOT / "release" / "DropGain-win64.zip"
THIRD_PARTY_FFMPEG = ROOT / "third_party" / "ffmpeg"
PACKAGING = ROOT / "packaging"


def ffmpeg_tool_path(name: str) -> Path | None:
    """Return path to a tool under third_party/ffmpeg/ (flat or bin/)."""
    for candidate in (THIRD_PARTY_FFMPEG / name, THIRD_PARTY_FFMPEG / "bin" / name):
        if candidate.is_file():
            return candidate
    return None


def require_ffmpeg() -> None:
    missing = [name for name in ("ffmpeg.exe", "ffprobe.exe") if ffmpeg_tool_path(name) is None]
    if missing:
        raise SystemExit(
            "Missing bundled FFmpeg tools in third_party/ffmpeg/ "
            "(or third_party/ffmpeg/bin/):\n"
            + "\n".join(f"  - {name}" for name in missing)
            + "\n\nSee third_party/ffmpeg/README.DropGain.txt"
        )


def run_pyinstaller() -> None:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(ROOT / "main.pyw"),
        "--name",
        "DropGain",
        "--onedir",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--paths",
        str(ROOT),
        "--add-data",
        f"fonts{os.pathsep}fonts",
        "--collect-all",
        "customtkinter",
        "--collect-all",
        "pedalboard",
    ]
    subprocess.run(cmd, cwd=ROOT, check=True)


def _copy_license_files(src_dir: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(src_dir.iterdir()):
        if not path.is_file():
            continue
        name_upper = path.name.upper()
        if (
            name_upper.startswith("COPYING")
            or name_upper.startswith("LICENSE")
            or path.suffix.lower() in {".txt", ".md"}
        ):
            if path.name.upper() == "README.TXT":
                continue
            shutil.copy2(path, dest_dir / path.name)


def write_first_run(dest: Path) -> None:
    src = PACKAGING / "FIRST_RUN.txt"
    if src.is_file():
        shutil.copy2(src, dest / "FIRST_RUN.txt")
        return
    dest.joinpath("FIRST_RUN.txt").write_text(
        "DropGain\n"
        "========\n\n"
        "1. Run DropGain.exe\n"
        "2. Optional limiter: download LoudMax VST3 from https://loudmax.blogspot.com/\n"
        "   and put LoudMax.vst3 in the plugins folder next to this file.\n"
        "3. Preferences > Check Limiter / System to verify ffmpeg and LoudMax.\n",
        encoding="utf-8",
    )


def assemble_release() -> None:
    if not DIST.is_dir():
        raise SystemExit(f"PyInstaller output missing: {DIST}")

    if RELEASE.exists():
        shutil.rmtree(RELEASE)
    RELEASE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(DIST, RELEASE)

    bin_dir = RELEASE / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        src = ffmpeg_tool_path(name)
        assert src is not None
        shutil.copy2(src, bin_dir / name)

    plugins = RELEASE / "plugins"
    plugins.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "plugins" / "README.txt", plugins / "README.txt")

    licenses = RELEASE / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", licenses / "LICENSE")
    _copy_license_files(THIRD_PARTY_FFMPEG, licenses / "ffmpeg")
    fonts_licenses = licenses / "fonts"
    fonts_licenses.mkdir(parents=True, exist_ok=True)
    ofl = ROOT / "fonts" / "OFL.txt"
    if ofl.is_file():
        shutil.copy2(ofl, fonts_licenses / "OFL.txt")
    mionta = ROOT / "fonts" / "mionta" / "license.txt"
    if mionta.is_file():
        shutil.copy2(mionta, fonts_licenses / "mionta-license.txt")

    write_first_run(RELEASE)


def zip_release() -> None:
    if RELEASE_ZIP.exists():
        RELEASE_ZIP.unlink()
    with zipfile.ZipFile(RELEASE_ZIP, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(RELEASE.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(RELEASE.parent).as_posix())


def main() -> None:
    parser = argparse.ArgumentParser(description="Build DropGain Windows onedir release.")
    parser.add_argument(
        "--zip",
        action="store_true",
        help=f"Also write {RELEASE_ZIP.name}",
    )
    parser.add_argument(
        "--skip-pyinstaller",
        action="store_true",
        help="Only assemble release/ from an existing dist/DropGain",
    )
    args = parser.parse_args()

    require_ffmpeg()
    if not args.skip_pyinstaller:
        run_pyinstaller()
    assemble_release()
    if args.zip:
        zip_release()
        print(f"Zip: {RELEASE_ZIP}")
    print(f"Ready: {RELEASE}")


if __name__ == "__main__":
    main()
