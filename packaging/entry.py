"""PyInstaller entry point (keeps the `connector` package imports relative).
The crash-loop guard runs first and imports only the stdlib-only guard: it records this start (and starts its heartbeat) BEFORE
`connector.main` is imported, so a build that cannot even import still counts its starts and is rolled back after two."""
import os
import sys

if __name__ == "__main__":
    if {"--install-repair", "--uninstall-repair"} & set(sys.argv[1:]):
        # elevated one-shot from the installer (UAC): no crash guard, no app, no single-instance check
        from connector import repair_setup
        sys.exit(repair_setup.main(sys.argv[1:]))
    if not {"--version", "--selftest", "--e2e"} & set(sys.argv[1:]):
        try:
            from pathlib import Path

            from connector import guard
            home = Path(os.environ.get("YC_HOME") or (os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")) + "\\YoussefConnector")
            ver = (Path(getattr(sys, "_MEIPASS", ".")) / "VERSION").read_text(encoding="utf-8").strip()
            if guard.begin(home, ver) == "rollback":
                sys.exit(0)
        except Exception:  # noqa: BLE001  (the guard must never block a start)
            pass
    from connector.main import main
    sys.exit(main())
