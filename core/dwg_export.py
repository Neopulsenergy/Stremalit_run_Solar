from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import ezdxf
from ezdxf.addons import odafc

logger = logging.getLogger("SolarLayoutGenerator")

ODA_INSTALL_URL = "https://www.opendesign.com/guestfiles/oda_file_converter"


class DWGExportError(RuntimeError):
    """Raised when DWG export is required (config dxf.require_dwg = true)
    but could not be produced."""

# ezdxf's built-in default Windows path assumes the installer creates
# "...\ODA\ODAFileConverter\ODAFileConverter.exe". Recent ODA installers
# instead create a version-suffixed folder, e.g.
# "...\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe", which the default
# won't match. Override it via (checked in this order):
#   1. configure_oda_path() called explicitly with a path
#   2. the ODA_FILE_CONVERTER_PATH environment variable
_CONFIGURED = False


def configure_oda_path(exe_path: Optional[str] = None) -> None:
    """
    Points ezdxf's odafc addon at a specific ODA File Converter executable.
    Call once at startup if it's installed somewhere other than ezdxf's
    hardcoded default location (common on Windows, where the install
    folder name includes the version number).
    """
    global _CONFIGURED
    path = exe_path or os.environ.get("ODA_FILE_CONVERTER_PATH")
    if path:
        ezdxf.options.set("odafc-addon", "win_exec_path", path)
        logger.info("ODA File Converter path configured: %s", path)
    _CONFIGURED = True


def oda_file_converter_available() -> bool:
    """True if the (free, separately-installed) ODA File Converter is
    present on this machine. DWG is a closed, proprietary binary format --
    no Python library can write it directly; this external tool is the
    standard way to convert DXF to a genuine DWG file."""
    if not _CONFIGURED:
        configure_oda_path()
    try:
        return odafc.is_installed()
    except Exception:
        return False


def export_dwg(doc, dwg_path: str, dxf_version: Optional[str] = None) -> dict:
    """
    Converts an in-memory ezdxf Drawing to a real, editable .dwg file via
    the ODA File Converter.

    Returns a status dict rather than raising on a missing converter, so a
    DWG-less environment still produces a usable DXF instead of crashing
    the whole pipeline:
        {"success": bool, "path": str | None, "message": str}
    """
    if not oda_file_converter_available():
        message = (
            "DWG not generated: the ODA File Converter isn't installed (or its path "
            "isn't configured) on this machine. It's a free, separate download (not "
            f"a Python package) from {ODA_INSTALL_URL} -- if it IS installed, set its "
            "exact .exe path via configure_oda_path() or the ODA_FILE_CONVERTER_PATH "
            "environment variable (recent installers use a version-suffixed folder "
            "name that ezdxf's default won't find automatically). In the meantime, "
            "the .dxf file opens and edits identically to a .dwg in AutoCAD -- use "
            "File > Save As > DWG there for a one-click local conversion."
        )
        logger.warning(message)
        return {"success": False, "path": None, "message": message}

    try:
        Path(dwg_path).parent.mkdir(parents=True, exist_ok=True)
        odafc.export_dwg(doc, dwg_path, version=dxf_version, replace=True)
        logger.info("DWG exported via ODA File Converter: %s", dwg_path)
        return {"success": True, "path": dwg_path, "message": f"DWG saved: {dwg_path}"}
    except Exception as exc:
        message = f"DWG export failed: {exc}"
        logger.warning(message)
        return {"success": False, "path": None, "message": message}

# End of core/dwg_export.py
