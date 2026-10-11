"""The "send logs" payload: the last 300 KB of connector.log and of crash.log with secrets removed. Pure functions (no network);
App.send_logs hands the text to the Connector, which sends it over the authenticated control socket as {"type": "logs", "text"}."""
import base64
import re
from pathlib import Path

TAIL = 300 * 1024
LOG_FILES = ("connector.log", "crash.log")

# the connect code is base64url({"u": hub url, "t": token}); the token travels in X-Connector-Token and in config.json
_RULES = (
    (re.compile(r"(?i)(x-connector-token|authorization|bearer|token|secret|password|api[_-]?key)(['\"]?\s*[:=]\s*['\"]?)(?:bearer\s+)?[^\s'\",}]+"),
     r"\1\2<redacted>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{16,}={0,2}"), "<redacted-code>"),      # a connect code or a JWT (both start with base64 of '{"')
    (re.compile(r"\b[A-Za-z0-9_-]{32,}\b"), "<redacted-long>"),               # any other long opaque string (tokens are 43 chars)
)


def redact(text: str, secrets=()) -> str:
    """Known secrets (the configured token and connect-code pieces) first, then the generic patterns."""
    for s in secrets:
        if s and len(str(s)) >= 4:
            text = text.replace(str(s), "<redacted>")
    for rx, rep in _RULES:
        text = rx.sub(rep, text)
    return text


def tail_text(path: Path, n: int = TAIL) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - n))
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def collect(home: Path, cfg: dict = None) -> str:
    """One text with a header per file: the tails of connector.log and crash.log, redacted."""
    cfg = cfg or {}
    secrets = [cfg.get("token")]
    if cfg.get("token") and cfg.get("url"):
        secrets.append(base64.urlsafe_b64encode(('{"u":"%s","t":"%s"}' % (cfg["url"], cfg["token"])).encode()).decode().rstrip("="))
    parts = []
    for name in LOG_FILES:
        body = redact(tail_text(Path(home) / name), secrets)
        parts.append(f"===== {name} (last {TAIL // 1024} KB) =====\n{body if body else '(missing or empty)'}\n")
    return "\n".join(parts)
