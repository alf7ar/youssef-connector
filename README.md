# Youssef Connector / موصل يوسف

**English.** A small Windows app that lets your dashboard place phone calls through your own PC and phone (Phone Link over
Bluetooth). It connects out to your dashboard, so no ports are opened.

1. Download and run: https://github.com/alf7ar/youssef-connector/releases/latest/download/YoussefConnector-Setup.exe
   (per-user install, no admin rights needed).
2. On first start a page opens at `http://127.0.0.1:5076/`. **Paste the connect code from your dashboard** ("Connect laptop").
3. Keep the PC awake with Phone Link connected to your phone. The page shows hub, phone readiness and the current call.

The app updates itself from this repository's Releases (SHA-256 verified; a mismatch keeps your current version).

**بالعربي.** تطبيق ويندوز صغير يخلي لوحة التحكم تعمل مكالمات من جهازك وموبايلك (Phone Link عن طريق البلوتوث). التطبيق
هو اللي بيتصل بلوحة التحكم، فمفيش منافذ بتتفتح.

1. حمّل وشغّل: https://github.com/alf7ar/youssef-connector/releases/latest/download/YoussefConnector-Setup.exe
   (تثبيت للمستخدم الحالي بدون صلاحيات أدمن).
2. أول مرة هتفتح صفحة على `http://127.0.0.1:5076/`. **الصق كود الربط من لوحة التحكم** ("ربط اللاب توب").
3. سيب الجهاز شغال و Phone Link متوصل بالموبايل. الصفحة بتعرض حالة الخادم والهاتف والمكالمة الحالية.

التطبيق بيتحدّث تلقائيا من Releases في المستودع ده (مع التحقق من SHA-256).

## Development

```
pip install -r packaging/requirements.txt pytest
python -m pytest -q            # core, app glue and updater tests run on Linux and Windows
python -m connector.main --selftest
```

`connector/core.py`, `connector/fakes.py`, `tests/mock_hub.py` and `tests/test_core.py` are synced from a private repository;
the Windows adapters are `connector/win_*.py`, `phone.py`, `micguard.py`, `readiness.py`, `vad.py`.
