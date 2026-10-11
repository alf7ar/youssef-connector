# PyInstaller spec (onedir): YoussefConnector.exe = packaging/entry.py + the connector package.
# Build (repo root, on Windows):  pyinstaller packaging/YoussefConnector.spec --noconfirm
import glob
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
PKG = os.path.join(ROOT, "connector")

datas, binaries, hiddenimports = [], [], []
for pkg in ("soundcard", "sounddevice", "soxr", "pycaw"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:  # noqa: BLE001
        print(f"spec: collect_all({pkg}) skipped: {e}")
hiddenimports += ["winrt.windows.applicationmodel.calls", "winrt.windows.devices.bluetooth", "winrt.windows.devices.radios",
                  "winrt.windows.devices.enumeration", "comtypes.client", "pycaw.pycaw", "pycaw.api.mmdeviceapi",
                  "pycaw.constants", "soundcard.mediafoundation"]
hiddenimports += collect_submodules("winrt") + collect_submodules("websockets") + ["comtypes", "psutil", "mock_hub"]
hiddenimports += collect_submodules("connector") + ["onnxruntime"]   # PyInstaller ships a hook for it; collect_all crashes on onnxruntime.quantization

# files read from disk at runtime: the VAD model + PowerShell dial helper (next to their modules), VERSION, the mock hub (--selftest)
for pattern in ("*.onnx", "*.ps1"):
    for f in glob.glob(os.path.join(PKG, pattern)):
        datas.append((f, "connector"))
datas.append((os.path.join(ROOT, "VERSION"), "."))
datas.append((os.path.join(ROOT, "tests", "mock_hub.py"), "tests"))

# app-local C++ runtime: a clean PC may lack msvcp140*.dll (onnxruntime needs a new enough copy)
sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
for dll in ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "vcruntime140.dll",
            "vcruntime140_1.dll", "concrt140.dll"):
    p = os.path.join(sysdir, dll)
    if os.path.exists(p):
        binaries.append((p, "."))

a = Analysis(
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT, os.path.join(ROOT, "tests")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="YoussefConnector",
    console=False,          # GUI-less background app; --selftest/--e2e write their result to --selftest-out
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="YoussefConnector", upx=False)
