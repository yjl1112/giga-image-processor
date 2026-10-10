"""Shared access-code authentication for Amazon Ops Flask applications."""

from __future__ import annotations

import hashlib
import hmac
import html
import os
from pathlib import Path
from shlex import split as shell_split
from typing import Any

from flask import Flask, Response, jsonify, make_response, redirect, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


ACCESS_COOKIE = "amazon_ops_access"
COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60
_SIGNING_SALT = "amazon-ops-access-v1"


def _load_env_token_from_file() -> str:
    """Read KROWORK_ACCESS_TOKEN from config/.env as a safety net.

    The unified launcher is expected to inject this variable into every Flask
    subprocess, but a stale long-running service, an external supervisor that
    forgot to forward the environment, or an out-of-band hub start can all
    leave a hub process without the code.  Without the token the hub's internal
    proxy never adds X-KroWork-Token to upstream requests, so every sub-service
    answers with the login HTML and the browser sees a non-JSON response.
    """
    if os.environ.get("KROWORK_ACCESS_TOKEN", "").strip():
        return ""
    try:
        from flask import has_app_context, current_app
        # Best-effort: app/config may live one or two levels above this file.
        candidates: list[Path] = []
        try:
            app_root = Path(current_app.root_path).resolve() if has_app_context() else None
        except Exception:
            app_root = None
        if app_root is not None:
            candidates.extend([app_root.parent.parent / "config" / ".env",
                               app_root.parent / "config" / ".env"])
        candidates.append(Path(__file__).resolve().parents[2] / "config" / ".env")
        candidates.append(Path(__file__).resolve().parents[3] / "config" / ".env")
        seen: set[Path] = set()
        for env_path in candidates:
            try:
                resolved = env_path.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            for raw in resolved.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().removeprefix("export ").strip()
                if key != "KROWORK_ACCESS_TOKEN":
                    continue
                parsed = shell_split(value.strip(), comments=False, posix=True)
                token = (parsed[0] if parsed else "").strip().strip("\"'")
                if token:
                    os.environ["KROWORK_ACCESS_TOKEN"] = token
                    return token
    except Exception:
        # Never let the safety-net loader break the host application.
        return ""
    return ""


_LOGIN_PAGE_CSS = """
:root {
    color-scheme: dark;
    font-family: Inter, "SF Pro Display", "Segoe UI", "PingFang SC",
        "Microsoft YaHei", sans-serif;
}

* {
    box-sizing: border-box;
}

html {
    min-width: 320px;
    background: #03101b;
}

body {
    margin: 0;
    min-height: 100vh;
    min-height: 100dvh;
    overflow: hidden;
    color: #f6fbff;
    background:
        radial-gradient(circle at 17% 18%, rgba(29, 105, 132, 0.30), transparent 29rem),
        radial-gradient(circle at 82% 78%, rgba(10, 84, 94, 0.22), transparent 32rem),
        linear-gradient(135deg, #020a12 0%, #061827 48%, #04111d 100%);
}

body::before {
    content: "";
    position: fixed;
    inset: -18%;
    z-index: 0;
    pointer-events: none;
    opacity: 0.58;
    background-image:
        radial-gradient(circle, rgba(218, 239, 244, 0.58) 0 1px, transparent 1.4px),
        radial-gradient(circle, rgba(118, 178, 194, 0.38) 0 1px, transparent 1.6px),
        radial-gradient(circle, rgba(255, 255, 255, 0.22) 0 0.8px, transparent 1.2px);
    background-position: 17px 29px, 61px 83px, 113px 41px;
    background-size: 127px 127px, 179px 179px, 223px 223px;
    transform: rotate(-4deg);
    animation: particle-drift 34s linear infinite;
}

body::after {
    content: "";
    position: fixed;
    inset: 0;
    z-index: 0;
    pointer-events: none;
    background:
        linear-gradient(90deg, rgba(0, 6, 12, 0.20), transparent 28%, transparent 72%, rgba(0, 6, 12, 0.20)),
        radial-gradient(ellipse at center, transparent 32%, rgba(0, 5, 10, 0.48) 100%);
}

@keyframes particle-drift {
    from { transform: rotate(-4deg) translate3d(0, 0, 0); }
    to { transform: rotate(-4deg) translate3d(-64px, 48px, 0); }
}

.ambient {
    position: fixed;
    inset: 0;
    z-index: 0;
    overflow: hidden;
    pointer-events: none;
}

.ambient::before,
.ambient::after {
    content: "";
    position: absolute;
    border: 1px solid rgba(126, 194, 205, 0.14);
    border-radius: 46% 54% 48% 52%;
    filter: blur(0.2px);
}

.ambient::before {
    width: 72vw;
    height: 72vw;
    left: -42vw;
    bottom: -47vw;
    box-shadow: 0 0 110px rgba(20, 119, 137, 0.12), inset 0 0 90px rgba(30, 131, 149, 0.08);
    transform: rotate(18deg);
}

.ambient::after {
    width: 46vw;
    height: 46vw;
    right: -31vw;
    top: -27vw;
    box-shadow: 0 0 90px rgba(27, 102, 143, 0.12), inset 0 0 70px rgba(28, 113, 147, 0.08);
    transform: rotate(-12deg);
}

.login-shell {
    position: relative;
    z-index: 1;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(360px, 430px);
    align-items: center;
    gap: clamp(48px, 8vw, 144px);
    width: min(1180px, calc(100% - 64px));
    min-height: 100vh;
    min-height: 100dvh;
    margin: 0 auto;
    padding: 56px 0;
}

.story {
    max-width: 650px;
    text-wrap: balance;
}

.eyebrow {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 0 0 26px;
    color: #b9d4dc;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.22em;
}

.eyebrow::before {
    content: "";
    width: 30px;
    height: 3px;
    border-radius: 999px;
    background: #f6c945;
    box-shadow: 0 0 22px rgba(246, 201, 69, 0.38);
}

.story h1 {
    max-width: 620px;
    margin: 0;
    color: #f4f9fb;
    font-family: Georgia, "Songti SC", "Noto Serif CJK SC", serif;
    font-size: clamp(42px, 5.2vw, 74px);
    font-weight: 400;
    letter-spacing: -0.035em;
    line-height: 1.14;
}

.story h1 span {
    color: #8ec0ca;
}

.story-copy {
    max-width: 540px;
    margin: 28px 0 0;
    color: #91aab4;
    font-size: 15px;
    letter-spacing: 0.035em;
    line-height: 1.9;
}

.box {
    position: relative;
    width: 100%;
    padding: 36px;
    overflow: hidden;
    border: 1px solid rgba(172, 211, 219, 0.19);
    border-radius: 22px;
    background: rgba(4, 18, 29, 0.72);
    box-shadow: 0 28px 80px rgba(0, 5, 10, 0.45), inset 0 1px 0 rgba(255, 255, 255, 0.035);
    backdrop-filter: blur(20px) saturate(120%);
    -webkit-backdrop-filter: blur(20px) saturate(120%);
}

.box::before {
    content: "";
    position: absolute;
    top: 0;
    left: 36px;
    width: 54px;
    height: 3px;
    border-radius: 0 0 999px 999px;
    background: #f6c945;
}

.box-kicker {
    margin: 0 0 12px;
    color: #79aab7;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.18em;
    text-transform: uppercase;
}

.box h2 {
    margin: 0;
    color: #f7fbfd;
    font-size: 25px;
    font-weight: 650;
    letter-spacing: -0.02em;
}

.intro {
    margin: 10px 0 26px;
    color: #8fa8b2;
    font-size: 13px;
    line-height: 1.65;
}

.field-label {
    display: block;
    margin: 0 0 9px;
    color: #c7d7dc;
    font-size: 12px;
    font-weight: 650;
}

input[type="password"] {
    width: 100%;
    min-height: 50px;
    margin: 0;
    padding: 0 15px;
    border: 1px solid rgba(146, 184, 194, 0.25);
    border-radius: 10px;
    outline: none;
    background: rgba(0, 8, 14, 0.58);
    color: #f7fbfd;
    caret-color: #f6c945;
    font: inherit;
    font-size: 15px;
    transition: border-color 160ms ease, box-shadow 160ms ease, background 160ms ease;
}

input[type="password"]::placeholder {
    color: #5f7882;
}

input[type="password"]:hover {
    border-color: rgba(157, 201, 211, 0.42);
}

input[type="password"]:focus-visible {
    border-color: #d6b84d;
    background: rgba(0, 8, 14, 0.76);
    box-shadow: 0 0 0 3px rgba(246, 201, 69, 0.12);
}

.error {
    margin: -10px 0 18px;
    padding: 10px 12px;
    border: 1px solid rgba(248, 113, 113, 0.22);
    border-radius: 9px;
    background: rgba(127, 29, 29, 0.16);
    color: #fecaca;
    font-size: 12px;
    line-height: 1.5;
}

button {
    display: inline-flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    min-height: 50px;
    margin-top: 16px;
    padding: 0 17px;
    border: 1px solid #f6c945;
    border-radius: 10px;
    background: #f6c945;
    color: #11191d;
    font: inherit;
    font-size: 14px;
    font-weight: 750;
    cursor: pointer;
    transition: transform 160ms ease, background 160ms ease, box-shadow 160ms ease;
}

button::after {
    content: "→";
    font-size: 19px;
    font-weight: 400;
    line-height: 1;
    transition: transform 160ms ease;
}

button:hover {
    background: #ffdc65;
    box-shadow: 0 10px 30px rgba(246, 201, 69, 0.14);
    transform: translateY(-1px);
}

button:hover::after {
    transform: translateX(3px);
}

button:focus-visible {
    outline: 3px solid rgba(255, 255, 255, 0.82);
    outline-offset: 3px;
}

.privacy-note {
    margin: 18px 0 0;
    color: #647f89;
    font-size: 11px;
    line-height: 1.55;
}

@media (max-width: 820px) {
    body {
        overflow: auto;
    }

    .login-shell {
        grid-template-columns: 1fr;
        align-content: center;
        gap: 36px;
        width: min(520px, calc(100% - 40px));
        padding: 48px 0;
    }

    .story h1 {
        font-size: clamp(36px, 9vw, 54px);
    }

    .story-copy {
        display: none;
    }
}

@media (max-width: 520px) {
    .login-shell {
        gap: 28px;
        width: min(100% - 28px, 430px);
        padding: 34px 0;
    }

    .eyebrow {
        margin-bottom: 18px;
    }

    .story h1 {
        font-size: clamp(33px, 10.5vw, 44px);
    }

    .box {
        padding: 30px 24px 26px;
        border-radius: 18px;
    }

    .box::before {
        left: 24px;
    }
}

@media (prefers-reduced-motion: reduce) {
    body::before {
        animation: none;
    }

    *,
    *::before,
    *::after {
        scroll-behavior: auto !important;
        transition-duration: 0.01ms !important;
    }
}
"""


def _access_token() -> str:
    token = os.environ.get("KROWORK_ACCESS_TOKEN", "").strip()
    if not token:
        token = _load_env_token_from_file()
    return token


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("KROWORK_SESSION_SECRET", "").strip()
    if not secret:
        # Rotation of the access code also invalidates existing sessions.
        secret = hashlib.sha256(
            f"{_access_token()}|{_SIGNING_SALT}".encode("utf-8")
        ).hexdigest()
    return URLSafeTimedSerializer(secret_key=secret, salt=_SIGNING_SALT)


def _safe_next_path(value: str | None) -> str:
    path = (value or "/").strip()
    return path if path.startswith("/") and not path.startswith("//") else "/"


def _cookie_kwargs() -> dict[str, Any]:
    host = request.host.split(":", 1)[0]
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
    kwargs: dict[str, Any] = {
        "httponly": True,
        "secure": request.is_secure or forwarded_proto == "https",
        "samesite": "Lax",
        "max_age": COOKIE_MAX_AGE_SECONDS,
        "path": "/",
    }
    if host.endswith("huichengds.dpdns.org"):
        kwargs["domain"] = ".huichengds.dpdns.org"
    return kwargs


def _login_page(error: str = "", next_path: str | None = None) -> Response:
    message = (
        f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    )
    raw_next = next_path if next_path is not None else request.path or "/"
    if raw_next in {"/auth/login", "/auth/logout"}:
        raw_next = "/"
    safe_next = html.escape(_safe_next_path(raw_next), quote=True)
    return Response(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="theme-color" content="#03101b">
    <title>Amazon Ops · 访问验证</title>
    <style>{_LOGIN_PAGE_CSS}</style>
</head>
<body>
    <div class="ambient" aria-hidden="true"></div>
    <main class="login-shell">
        <section class="story" aria-labelledby="welcome-title">
            <p class="eyebrow">AMAZON OPERATIONS</p>
            <h1 id="welcome-title">让每一次运营决策，<br><span>都有清晰的方向。</span></h1>
            <p class="story-copy">从商品、库存到广告与履约，在同一个工作台穿过数据噪音，找到下一步行动。</p>
        </section>
        <form class="box" method="post" action="/auth/login">
            <p class="box-kicker">Secure access</p>
            <h2>输入访问码</h2>
            <p class="intro">这是 Amazon 运营系统公网入口，请完成验证后继续。</p>
            {message}
            <label class="field-label" for="access-token">访问码</label>
            <input id="access-token" name="token" type="password" required autofocus autocomplete="current-password" placeholder="请输入访问码">
            <input type="hidden" name="next" value="{safe_next}">
            <button type="submit">进入运营系统</button>
            <p class="privacy-note">验证信息仅用于当前浏览器会话，不会显示在页面中。</p>
        </form>
    </main>
</body>
</html>""",
        status=401,
        mimetype="text/html",
    )


def _valid_header_token(value: str | None) -> bool:
    token = _access_token()
    return bool(value and token and hmac.compare_digest(value, token))


def _valid_session(value: str | None) -> bool:
    if not value or not _access_token():
        return False
    try:
        return _serializer().loads(value, max_age=COOKIE_MAX_AGE_SECONDS) == {"authenticated": True}
    except (BadSignature, SignatureExpired):
        return False


def configure_access(app: Flask, public_path_prefixes: tuple[str, ...] = ()) -> None:
    """Add the shared browser login flow while retaining internal token headers."""

    @app.before_request
    def require_access():
        token = _access_token()
        if not token:
            # The unified launcher refuses public startup without this variable.
            # Keeping local no-token mode preserves isolated development and tests.
            return None
        if request.path in {"/auth/login", "/auth/logout"}:
            return None
        if any(request.path.startswith(prefix) for prefix in public_path_prefixes):
            return None
        if _valid_session(request.cookies.get(ACCESS_COOKIE)) or _valid_header_token(request.headers.get("X-KroWork-Token")):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "需要访问码"}), 401
        return _login_page()

    @app.get("/auth/login")
    def auth_login_page():
        return _login_page()

    @app.post("/auth/login")
    def auth_login():
        if not _valid_header_token(request.form.get("token")):
            return _login_page("访问码不正确", request.form.get("next"))
        response = make_response(redirect(_safe_next_path(request.form.get("next"))))
        response.set_cookie(
            ACCESS_COOKIE,
            _serializer().dumps({"authenticated": True}),
            **_cookie_kwargs(),
        )
        return response

    @app.post("/auth/logout")
    def auth_logout():
        response = make_response(redirect("/"))
        response.delete_cookie(ACCESS_COOKIE, **_cookie_kwargs())
        return response
