import os
import sys
import logging
import shlex
import subprocess
import threading
import time
import winreg
import webbrowser

from pathlib import Path
from typing import List, Optional

import pystray
from PIL import Image

# ─── Constants ────────────────────────────────────────────────────────────────

APP_NAME = "uxplay-windows"
APPDATA_DIR = Path(os.environ["APPDATA"]) / "uxplay-windows"
LOG_FILE = APPDATA_DIR / f"{APP_NAME}.log"

# ensure the AppData folder exists up front:
APPDATA_DIR.mkdir(parents=True, exist_ok=True)

# ─── Logging Setup ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

# ─── Path Discovery ───────────────────────────────────────────────────────────

class Paths:
    """
    Find where our bundled resources live:
      • if PyInstaller one-file: sys._MEIPASS
      • else if one-dir: same folder as the exe
      • else (running from .py): the script's folder
    Then, if there is an `_internal` subfolder, use that.
    """
    def __init__(self):
        if getattr(sys, "frozen", False):
            # one-file mode unpacks to _MEIPASS
            if hasattr(sys, "_MEIPASS"):
                cand = Path(sys._MEIPASS)
            else:
                # one-dir mode: resources sit beside the exe
                cand = Path(sys.executable).parent
        else:
            cand = Path(__file__).resolve().parent

        # if there's an _internal subfolder, that's where our .ico + bin live
        internal = cand / "_internal"
        self.resource_dir = internal if internal.is_dir() else cand

        # icon is directly in resource_dir
        self.icon_file = self.resource_dir / "icon.ico"

        # first look for bin/uxplay.exe, else uxplay.exe at top level
        ux1 = self.resource_dir / "bin" / "uxplay.exe"
        ux2 = self.resource_dir / "uxplay.exe"
        self.uxplay_exe = ux1 if ux1.exists() else ux2

        # bundled mDNSResponder for portable use (may not exist if Bonjour is system-installed)
        self.mdns_exe = self.resource_dir / "bin" / "mDNSResponder.exe"

        # AppData paths
        self.appdata_dir = APPDATA_DIR
        self.arguments_file = self.appdata_dir / "arguments.txt"

# ─── Argument File Manager ────────────────────────────────────────────────────

class ArgumentManager:
    def __init__(self, file_path: Path):
        self.file_path = file_path

    def ensure_exists(self) -> None:
        logging.info("Ensuring arguments file at '%s'", self.file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self.file_path.write_text("", encoding="utf-8")
            logging.info("Created empty arguments.txt")

    def read_args(self) -> List[str]:
        if not self.file_path.exists():
            logging.warning("arguments.txt missing → no custom args")
            return []
        text = self.file_path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        try:
            return shlex.split(text)
        except ValueError as e:
            logging.error("Could not parse arguments.txt: %s", e)
            return []

# ─── mDNSResponder Manager ────────────────────────────────────────────────────

class MdnsManager:
    """
    Manages a bundled mDNSResponder.exe for portable use.

    If the Bonjour service is already running system-wide we skip launching
    our own copy to avoid conflicts.  If the bundled executable is missing we
    log a warning and carry on (the user may have Bonjour installed separately).
    """

    BONJOUR_SERVICE = "Bonjour Service"

    def __init__(self, exe_path: Path):
        self.exe_path = exe_path
        self.process: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------
    def _bonjour_service_running(self) -> bool:
        """Return True if the Windows Bonjour service is already running."""
        try:
            result = subprocess.run(
                ["sc", "query", self.BONJOUR_SERVICE],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return "RUNNING" in result.stdout
        except Exception:
            return False

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._bonjour_service_running():
            logging.info("Bonjour service already running – skipping bundled mDNSResponder")
            return

        if not self.exe_path.exists():
            logging.warning(
                "Bundled mDNSResponder not found at %s. "
                "mDNS discovery may not work unless Bonjour is installed.",
                self.exe_path,
            )
            return

        if self.process and self.process.poll() is None:
            logging.info("mDNSResponder already running (PID %s)", self.process.pid)
            return

        logging.info("Starting bundled mDNSResponder: %s", self.exe_path)
        try:
            self.process = subprocess.Popen(
                [str(self.exe_path)],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            # Give the process a moment to start (or fail immediately)
            time.sleep(1)
            if self.process.poll() is not None:
                logging.warning(
                    "mDNSResponder exited immediately (code %s). "
                    "mDNS discovery may not work on this machine without "
                    "the Bonjour service installed.",
                    self.process.returncode,
                )
                self.process = None
            else:
                logging.info("Started mDNSResponder (PID %s)", self.process.pid)
        except Exception:
            logging.exception("Failed to launch mDNSResponder")

    # ------------------------------------------------------------------
    def stop(self) -> None:
        if not (self.process and self.process.poll() is None):
            return
        pid = self.process.pid
        logging.info("Stopping mDNSResponder (PID %s)…", pid)
        try:
            self.process.terminate()
            self.process.wait(timeout=3)
            logging.info("mDNSResponder stopped.")
        except subprocess.TimeoutExpired:
            logging.warning("mDNSResponder did not stop in time; killing it.")
            self.process.kill()
            self.process.wait()
        except Exception:
            logging.exception("Error stopping mDNSResponder")
        finally:
            self.process = None


# ─── Server Process Manager ──────────────────────────────────────────────────

class ServerManager:
    def __init__(self, exe_path: Path, arg_mgr: ArgumentManager):
        self.exe_path = exe_path
        self.arg_mgr = arg_mgr
        self.process: Optional[subprocess.Popen] = None

    def _build_env(self) -> dict:
        """Build an environment suitable for running uxplay.exe portably."""
        env = os.environ.copy()

        bin_dir = self.exe_path.parent
        internal_dir = bin_dir.parent

        # Ensure the bundled DLL directory is first in PATH so Windows finds
        # the bundled copies before any system-wide DLLs.
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

        # GStreamer plugin path – mingw64 gstreamer looks for plugins relative
        # to its DLL (../lib/gstreamer-1.0), but set the env var explicitly to
        # be safe when the layout differs.
        gst_plugin_path = internal_dir / "lib" / "gstreamer-1.0"
        if gst_plugin_path.exists():
            env["GST_PLUGIN_PATH"] = str(gst_plugin_path)
            env["GST_PLUGIN_SYSTEM_PATH"] = str(gst_plugin_path)
            logging.debug("GST_PLUGIN_PATH → %s", gst_plugin_path)

        return env

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            logging.info("UxPlay server already running (PID %s)", self.process.pid)
            return

        if not self.exe_path.exists():
            logging.error("uxplay.exe not found at %s", self.exe_path)
            return

        cmd = [str(self.exe_path)] + self.arg_mgr.read_args()
        logging.info("Starting UxPlay: %s", cmd)
        try:
            self.process = subprocess.Popen(
                cmd,
                env=self._build_env(),
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            logging.info("Started UxPlay (PID %s)", self.process.pid)
        except Exception:
            logging.exception("Failed to launch UxPlay")

    def stop(self) -> None:
        if not (self.process and self.process.poll() is None):
            logging.info("UxPlay server not running.")
            return

        pid = self.process.pid
        logging.info("Stopping UxPlay (PID %s)...", pid)
        try:
            self.process.terminate()
            self.process.wait(timeout=3)
            logging.info("UxPlay stopped cleanly.")
        except subprocess.TimeoutExpired:
            logging.warning("Did not terminate in time; killing it.")
            self.process.kill()
            self.process.wait()
        except Exception:
            logging.exception("Error stopping UxPlay")
        finally:
            self.process = None

# ─── Auto-Start Manager ───────────────────────────────────────────────────────

class AutoStartManager:
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def __init__(self, app_name: str, exe_cmd: str):
        self.app_name = app_name
        self.exe_cmd = exe_cmd

    def is_enabled(self) -> bool:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                self.RUN_KEY,
                0,
                winreg.KEY_READ
            ) as key:
                val, _ = winreg.QueryValueEx(key, self.app_name)
                return self.exe_cmd in val
        except FileNotFoundError:
            return False
        except Exception:
            logging.exception("Error checking Autostart")
            return False

    def enable(self) -> None:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                self.RUN_KEY,
                0,
                winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(
                    key,
                    self.app_name,
                    0,
                    winreg.REG_SZ,
                    self.exe_cmd
                )
            logging.info("Autostart enabled")
        except Exception:
            logging.exception("Failed to enable Autostart")

    def disable(self) -> None:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                self.RUN_KEY,
                0,
                winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, self.app_name)
            logging.info("Autostart disabled")
        except FileNotFoundError:
            logging.info("No Autostart entry to delete")
        except Exception:
            logging.exception("Failed to disable Autostart")

    def toggle(self) -> None:
        if self.is_enabled():
            self.disable()
        else:
            self.enable()

# ─── System Tray Icon UI ─────────────────────────────────────────────────────

class TrayIcon:
    def __init__(
        self,
        icon_path: Path,
        server_mgr: ServerManager,
        arg_mgr: ArgumentManager,
        auto_mgr: AutoStartManager
    ):
        self.server_mgr = server_mgr
        self.arg_mgr = arg_mgr
        self.auto_mgr = auto_mgr

        menu = pystray.Menu(
            pystray.MenuItem("Start UxPlay", lambda _: server_mgr.start()),
            pystray.MenuItem("Stop UxPlay",  lambda _: server_mgr.stop()),
            pystray.MenuItem("Restart UxPlay", lambda _: self._restart()),
            pystray.MenuItem(
                "Autostart with Windows",
                lambda _: auto_mgr.toggle(),
                checked=lambda _: auto_mgr.is_enabled()
            ),
            pystray.MenuItem(
                "Edit UxPlay Arguments",
                lambda _: self._open_args()
            ),
            pystray.MenuItem(
                "License",
                lambda _: webbrowser.open(
                    "https://github.com/leapbtw/uxplay-windows/blob/"
                    "main/LICENSE.md"
                )
            ),
            pystray.MenuItem("Exit", lambda _: self._exit())
        )

        self.icon = pystray.Icon(
            name=f"{APP_NAME}\nRight-click to configure.",
            icon=Image.open(icon_path),
            title=APP_NAME,
            menu=menu
        )

    def _restart(self):
        logging.info("Restarting UxPlay")
        self.server_mgr.stop()
        self.server_mgr.start()

    def _open_args(self):
        self.arg_mgr.ensure_exists()
        try:
            os.startfile(str(self.arg_mgr.file_path))
            logging.info("Opened arguments.txt")
        except Exception:
            logging.exception("Failed to open arguments.txt")

    def _exit(self):
        logging.info("Exiting tray")
        self.server_mgr.stop()
        self.icon.stop()
        # MdnsManager.stop() is called from Application after the tray exits

    def run(self):
        self.icon.run()

# ─── Application Orchestration ───────────────────────────────────────────────

class Application:
    def __init__(self):
        self.paths = Paths()
        self.arg_mgr = ArgumentManager(self.paths.arguments_file)
        self.mdns_mgr = MdnsManager(self.paths.mdns_exe)

        # Build the exact command string for registry
        script = Path(__file__).resolve()
        if getattr(sys, "frozen", False):
            exe_cmd = f'"{sys.executable}"'
        else:
            exe_cmd = f'"{sys.executable}" "{script}"'

        self.auto_mgr = AutoStartManager(APP_NAME, exe_cmd)
        self.server_mgr = ServerManager(self.paths.uxplay_exe, self.arg_mgr)
        self.tray      = TrayIcon(
            self.paths.icon_file,
            self.server_mgr,
            self.arg_mgr,
            self.auto_mgr
        )

    def run(self):
        self.arg_mgr.ensure_exists()

        # delay server start so the tray icon appears immediately
        threading.Thread(target=self._delayed_start, daemon=True).start()

        logging.info("Launching tray icon")
        self.tray.run()
        logging.info("Tray exited – shutting down")

        # Clean up mDNSResponder after the tray loop exits
        self.mdns_mgr.stop()

    def _delayed_start(self):
        # Start mDNSResponder first so it is ready before uxplay.exe connects
        self.mdns_mgr.start()
        time.sleep(2)
        self.server_mgr.start()

if __name__ == "__main__":
    Application().run()
