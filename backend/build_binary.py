"""
PyInstaller build script for creating the standalone Python server binary.

JFW-1 (jf-whisper-Profil): Transkriptions-Build — der Server bundelt nur den
Whisper-STT-Kern (torch + transformers). Alle TTS-/LLM-Pakete (qwen_tts,
chatterbox, kokoro, zipvoice, tada, hume) und der MCP-Server sind aus dem
Profil ausgeschlossen und werden NICHT gebundlet.

Usage:
    python build_binary.py           # Build default (CPU) server binary
    python build_binary.py --cuda    # Build CUDA-enabled server binary
"""

import PyInstaller.__main__
import argparse
import logging
import os
import platform
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def is_apple_silicon():
    """Check if running on Apple Silicon."""
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def build_server(cuda=False):
    """Build Python server as standalone binary.

    Args:
        cuda: If True, build with CUDA support and name the binary
              jf-whisper-server-cuda instead of jf-whisper-server.
    """
    backend_dir = Path(__file__).parent

    binary_name = "jf-whisper-server-cuda" if cuda else "jf-whisper-server"

    # PyInstaller arguments
    # CUDA builds use --onedir so we can split the output into two archives:
    #   1. Server core (~200-400MB) — versioned with the app
    #   2. CUDA libs (~2GB) — versioned independently (only redownloaded on
    #      CUDA toolkit / torch major version changes)
    # CPU builds remain --onefile for simplicity.
    pack_mode = "--onedir" if cuda else "--onefile"
    args = [
        "server.py",  # Use server.py as entry point instead of main.py
        pack_mode,
        "--name",
        binary_name,
    ]

    # Hide console window on Windows only. On macOS/Linux the sidecar needs
    # stdout/stderr for Tauri to capture logs.
    if platform.system() == "Windows":
        args.append("--noconsole")

    # numpy 2.x / torch ABI mismatch fix: install memmove fallback for
    # torch.from_numpy() before the app starts. Runtime hooks run after
    # FrozenImporter is registered so frozen torch/numpy are importable.
    # Paths are passed relative to backend_dir because os.chdir(backend_dir)
    # runs before PyInstaller. Absolute paths would get baked into the
    # generated .spec, breaking reproducible builds on other machines / CI.
    args.extend(
        [
            "--runtime-hook",
            "pyi_rth_numpy_compat.py",
            # Stub torch.compiler.disable before transformers imports
            # flex_attention, which otherwise triggers torch._dynamo →
            # torch._numpy._ufuncs and crashes at module load under
            # PyInstaller. See pyi_rth_torch_compiler_disable.py.
            "--runtime-hook",
            "pyi_rth_torch_compiler_disable.py",
            # Per-module collection overrides: transformers.masking_utils muss
            # als .py-Quelle gebundlet werden, damit der Runtime-Hook es patchen
            # kann (Whisper-Pfad); scipy.stats._distn_infrastructure analog fuer
            # den librosa/STT-Audio-Pfad.
            "--additional-hooks-dir",
            "pyi_hooks",
        ]
    )

    # Add common hidden imports — STT-Kern (Whisper) only.
    args.extend(
        [
            "--hidden-import",
            "backend",
            "--hidden-import",
            "backend.main",
            "--hidden-import",
            "backend.config",
            "--hidden-import",
            "backend.database",
            # JFW-1 DB-Schema-Linie (CPU-Early-Entrypoint): Alembic + die
            # Revisionen werden als Datei geladen, daher zusätzlich als Daten.
            "--hidden-import",
            "backend.schema",
            "--collect-all",
            "alembic",
            "--hidden-import",
            "backend.models",
            "--hidden-import",
            "backend.services.transcribe",
            "--hidden-import",
            "backend.utils.platform_detect",
            "--hidden-import",
            "backend.backends",
            "--hidden-import",
            "backend.backends.pytorch_backend",
            "--hidden-import",
            "backend.utils.audio",
            "--hidden-import",
            "backend.utils.cache",
            "--hidden-import",
            "backend.utils.progress",
            "--hidden-import",
            "backend.utils.hf_progress",
            "--hidden-import",
            "torch",
            "--hidden-import",
            "transformers",
            "--hidden-import",
            "fastapi",
            "--hidden-import",
            "uvicorn",
            "--hidden-import",
            "sqlalchemy",
            # librosa uses lazy_loader which generates .pyi stub files at
            # install time and reads them at runtime to discover submodules.
            # --hidden-import alone doesn't bundle the stubs, causing
            # "Cannot load imports from non-existent stub" at runtime.
            "--collect-all",
            "lazy_loader",
            "--collect-all",
            "librosa",
            "--hidden-import",
            "soundfile",
            "--copy-metadata",
            "requests",
            "--copy-metadata",
            "transformers",
            "--copy-metadata",
            "huggingface-hub",
            "--copy-metadata",
            "tokenizers",
            "--copy-metadata",
            "safetensors",
            "--copy-metadata",
            "tqdm",
            "--hidden-import",
            "requests",
            # Fix for pkg_resources and jaraco namespace packages
            "--hidden-import",
            "pkg_resources.extern",
            "--collect-submodules",
            "jaraco",
        ]
    )

    # Add CUDA-specific hidden imports
    if cuda:
        logger.info("Building with CUDA support")
        args.extend(
            [
                "--hidden-import",
                "torch.cuda",
                "--hidden-import",
                "torch.backends.cudnn",
            ]
        )
    else:
        # Exclude NVIDIA CUDA packages from CPU-only builds to keep binary small.
        # When building from a venv with CUDA torch installed, PyInstaller would
        # bundle ~3GB of NVIDIA shared libraries. We exclude both the Python
        # modules and the binary DLLs.
        nvidia_packages = [
            "nvidia",
            "nvidia.cublas",
            "nvidia.cuda_cupti",
            "nvidia.cuda_nvrtc",
            "nvidia.cuda_runtime",
            "nvidia.cudnn",
            "nvidia.cufft",
            "nvidia.curand",
            "nvidia.cusolver",
            "nvidia.cusparse",
            "nvidia.nccl",
            "nvidia.nvjitlink",
            "nvidia.nvtx",
        ]
        for pkg in nvidia_packages:
            args.extend(["--exclude-module", pkg])

    # JFW-1: TTS-/LLM-Pakete werden explizit ausgeschlossen — selbst wenn sie
    # im Build-Venv installiert sind, duerfen sie nicht ins Binary. Das ist die
    # Build-Seite des ACs "keine Runtime-Abhaengigkeiten, die nur fuer TTS
    # gebraucht werden".
    tts_packages = [
        "qwen_tts",
        "chatterbox",
        "kokoro",
        "zipvoice",
        "tada",
        "misaki",
        "pedalboard",
        "perth",
        "piper_phonemize",
        "inflect",
        "spacy_pkuseg",
        "linacodec",
    ]
    for pkg in tts_packages:
        args.extend(["--exclude-module", pkg])

    # MLX (Apple Silicon) — STT-only.
    if is_apple_silicon() and not cuda:
        logger.info("Building for Apple Silicon - including MLX dependencies")
        args.extend(
            [
                "--hidden-import",
                "backend.backends.mlx_backend",
                "--hidden-import",
                "mlx",
                "--hidden-import",
                "mlx.core",
                "--collect-submodules",
                "mlx",
                # Use --collect-all so PyInstaller bundles both data files AND
                # native shared libraries (.dylib, .metallib) for MLX.
                "--collect-all",
                "mlx",
            ]
        )
    elif not cuda:
        logger.info("Building for non-Apple Silicon platform - PyTorch only")

    dist_dir = str(backend_dir / "dist")
    build_dir = str(backend_dir / "build")

    # JFW-1 DB-Schema-Linie: alembic.ini + versions/ werden zur Laufzeit aus
    # der Datei geladen (ScriptDirectory) und müssen daher als Daten gebundlet
    # werden. WICHTIG: das Ziel ist `backend/...`, damit es im Frozen-Layout
    # exakt dort liegt, wo schema.py hinschaut — Path(__file__).parent ist im
    # Onefile-Modus `_MEIPASS/backend/` (das backend-Paket), nicht die _MEIPASS-
    # Wurzel. Quellbaum und Binary müssen denselben relativen Layout haben.
    args.extend(
        [
            "--add-data",
            f"{backend_dir / 'alembic.ini'}{os.pathsep}backend/alembic.ini",
            # Komplettes alembic/-Verzeichnis (env.py + versions/) — Alembic
            # laedt env.py aus dem ScriptDirectory, nicht nur die Revisionen.
            "--add-data",
            f"{backend_dir / 'alembic'}{os.pathsep}backend/alembic",
        ]
    )

    args.extend(
        [
            "--distpath",
            dist_dir,
            "--workpath",
            build_dir,
            "--noconfirm",
            "--clean",
        ]
    )

    # Change to backend directory
    os.chdir(backend_dir)

    # For CPU builds on Windows, ensure we're using CPU-only torch.
    # If CUDA torch is installed (local dev), swap to CPU torch before building,
    # then restore CUDA torch after. This prevents PyInstaller from bundling
    # ~3GB of CUDA DLLs into the CPU binary.
    restore_cuda = False
    if not cuda and platform.system() == "Windows":
        import subprocess

        result = subprocess.run(
            [sys.executable, "-c", "import torch; print(torch.version.cuda or '')"], capture_output=True, text=True
        )
        has_cuda_torch = bool(result.stdout.strip())
        if has_cuda_torch:
            logger.info("CUDA torch detected — installing CPU torch for CPU build...")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "torch",
                    "--index-url",
                    "https://download.pytorch.org/whl/cpu",
                    "--force-reinstall",
                    "-q",
                ],
                check=True,
            )
            restore_cuda = True

    # Run PyInstaller
    try:
        PyInstaller.__main__.run(args)
    finally:
        # Restore CUDA torch if we swapped it out (even on build failure)
        if restore_cuda:
            logger.info("Restoring CUDA torch...")
            import subprocess

            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "torch",
                    "--index-url",
                    "https://download.pytorch.org/whl/cu128",
                    "--force-reinstall",
                    "-q",
                ],
                check=True,
            )

    logger.info("Binary built in %s", backend_dir / "dist" / binary_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build voicebox binaries")
    parser.add_argument(
        "--cuda",
        action="store_true",
        help="Build CUDA-enabled binary (jf-whisper-server-cuda)",
    )
    cli_args = parser.parse_args()
    build_server(cuda=cli_args.cuda)
