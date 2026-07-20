"""Build a Windows onedir DropGain release folder.

Prerequisites:
  python -m venv .venv
  .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt -r requirements-build.txt
  Place an LGPL FFmpeg essentials build in third_party/ffmpeg/ (see README.DropGain.txt).

Run:
  build.bat
  or:  python build.py
(build.py re-launches itself with .venv\\Scripts\\python.exe when needed.)

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
VENV_PYTHON = (
    ROOT / ".venv" / "Scripts" / "python.exe"
    if os.name == "nt"
    else ROOT / ".venv" / "bin" / "python"
)
# Not used by DropGain. Needed when building from a polluted global Python;
# a clean venv makes most of this unnecessary. exclude-module alone does not
# always block native DLL collection, so also keep CUDA/ML stacks out.
EXCLUDE_MODULES = (
    # GUI toolkits (DropGain is tk/customtkinter only)
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "shiboken2",
    "shiboken6",
    "wx",
    # ML / GPU (seen bloating release/ to ~2GB)
    "torch",
    "torchaudio",
    "torchvision",
    "tensorflow",
    "keras",
    "cupy",
    "cupy_backends",
    "cupyx",
    "cuda",
    "cuda_bindings",
    "numba",
    "triton",
    # Dev / notebook / lint
    "pytest",
    "_pytest",
    "py",
    "IPython",
    "ipykernel",
    "ipywidgets",
    "jupyter",
    "jupyter_client",
    "jupyter_core",
    "notebook",
    "nbformat",
    "nbconvert",
    "black",
    "yapf",
    "jedi",
    "parso",
    "astroid",
    "pylint",
    # Data / viz / games not used by DropGain
    "matplotlib",
    "pygame",
    "cv2",
    "sklearn",
    "pandas",
    "h5py",
    "tables",
    # Network / DB / crypto stacks pulled transitively from global env
    "sqlalchemy",
    "alembic",
    "sqlcipher3",
    "MySQLdb",
    "pymysql",
    "psycopg2",
    "grpc",
    "grpcio",
    "google",
    "cryptography",
    "bcrypt",
    "nacl",
    "paramiko",
    "urllib3",
    "requests",
    "certifi",
    "aiohttp",
    "zmq",
    "jsonschema",
    "cloudpickle",
    "pygments",
    "traitlets",
    "mako",
    "jinja2",
    "lxml",
    "psutil",
    "clr",
    "clr_loader",
    "pythonnet",
)

# If these import, the freeze will almost certainly ship hundreds of MB of junk.
POLLUTION_CHECK_MODULES = (
    "torch",
    "cupy",
    "tensorflow",
    "PyQt5",
    "PyQt6",
    "sqlalchemy",
    "IPython",
)


def ensure_venv_python() -> None:
    """Require repo .venv and re-exec under it when launched with another Python."""
    if not VENV_PYTHON.is_file():
        raise SystemExit(
            "Missing .venv. Create it first:\n"
            "  python -m venv .venv\n"
            "  .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt -r requirements-build.txt\n"
            "Then run build.bat or: python build.py"
        )
    if Path(sys.executable).resolve() == VENV_PYTHON.resolve():
        return
    print(f"Re-launching with {VENV_PYTHON}", flush=True)
    raise SystemExit(subprocess.call([str(VENV_PYTHON), str(ROOT / "build.py"), *sys.argv[1:]]))


def warn_if_polluted_env() -> None:
    """Warn when the active interpreter has packages that bloat Windows freezes."""
    import importlib.util

    found = [name for name in POLLUTION_CHECK_MODULES if importlib.util.find_spec(name) is not None]
    in_venv = sys.prefix != sys.base_prefix or bool(os.environ.get("VIRTUAL_ENV"))
    if not found and in_venv:
        return
    print()
    if not in_venv:
        print("WARNING: not building inside a venv (sys.prefix == system Python).")
    if found:
        print("WARNING: polluted build environment; these importable packages bloat the freeze:")
        for name in found:
            print(f"  - {name}")
    print(
        "Prefer:\n"
        "  python -m venv .venv\n"
        "  .\\.venv\\Scripts\\Activate.ps1\n"
        "  pip install -r requirements.txt -r requirements-build.txt\n"
        "  python build.py\n"
    )
    print()


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
        "--collect-binaries",
        "pedalboard",
        "--collect-data",
        "pedalboard",
    ]
    for mod in EXCLUDE_MODULES:
        cmd.extend(["--exclude-module", mod])
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
    ensure_venv_python()
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
        warn_if_polluted_env()
        run_pyinstaller()
    assemble_release()
    if args.zip:
        zip_release()
        print(f"Zip: {RELEASE_ZIP}")
    print(f"Ready: {RELEASE}")


if __name__ == "__main__":
    main()
