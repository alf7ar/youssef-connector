"""PyInstaller entry point (keeps the `connector` package imports relative).
The crash-loop check runs first and imports only the stdlib-only guard, so a build that cannot even import is still rolled back."""
import os
import sys

if __name__ == "__main__":
    if not {"--version", "--selftest", "--e2e"} & set(sys.argv[1:]):
        try:
            from pathlib import Path

            from connector import guard
            home = Path(os.environ.get("YC_HOME") or (os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")) + "\\YoussefConnector")
            ver = (Path(getattr(sys, "_MEIPASS", ".")) / "VERSION").read_text(encoding="utf-8").strip()
            if guard.frozen() and guard.rollback_due(home, ver) and guard.rollback(home, ver):
                sys.exit(0)
        except Exception:  # noqa: BLE001  (the guard must never block a start)
            pass
    from connector.main import main
    sys.exit(main())
