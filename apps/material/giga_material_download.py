"""Download GIGA product resource packages through Kimi WebBridge.

This module deliberately does not contain Flask routes.  The material app can
create a run directory, call :class:`GigaMaterialDownloader`, then hand the
downloaded ZIP files to its existing image-processing workflow.

The browser remains the source of truth for GIGA authentication.  In
particular, CAPTCHA / Safe Checker pages are reported as a terminal
``needs_verification`` result; this code never attempts to solve, bypass, or
retry a verification challenge.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol
from urllib.parse import quote, urljoin, urlparse


DEFAULT_DAEMON_URL = os.environ.get(
    "KIMI_WEBBRIDGE_URL", "http://127.0.0.1:10086/command"
).strip()
DEFAULT_GIGA_BASE_URL = os.environ.get("GIGA_WEB_BASE_URL", "https://www.gigab2b.com").strip()
DEFAULT_SESSION = "giga-material-download"

ITEM_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
PARTIAL_DOWNLOAD_SUFFIXES = (".crdownload", ".part", ".partial", ".tmp", ".download")
# Keep these header-only limits aligned with the protected extractor in
# ``app_bundle/scripts/process_giga_materials.py``.  The downloader must not
# call ``ZipFile.testzip()`` here: that would inflate every member before the
# extractor can apply its resource limits.
MAX_DOWNLOAD_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_DOWNLOAD_ZIP_MEMBERS = 5000
MAX_DOWNLOAD_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_DOWNLOAD_MEMBER_BYTES = 512 * 1024 * 1024
MAX_DOWNLOAD_COMPRESSION_RATIO = 1000
# Kimi operates one browser tab at a time.  This lock is deliberately module
# scoped so separate downloader instances (for example, one per Flask job)
# still serialize their browser interactions within the service process.
_GLOBAL_DOWNLOAD_LOCK = threading.Lock()


class GigaMaterialDownloadError(RuntimeError):
    """A user-displayable material-download error with a stable error code."""

    code = "download_failed"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.details}


class InvalidItemCodeError(GigaMaterialDownloadError):
    code = "invalid_item_code"


class InvalidDownloadOptionError(GigaMaterialDownloadError):
    code = "invalid_download_option"


class KimiWebBridgeError(GigaMaterialDownloadError):
    code = "webbridge_unavailable"


class GigaLoginRequired(GigaMaterialDownloadError):
    code = "needs_login"


class GigaVerificationRequired(GigaMaterialDownloadError):
    code = "needs_verification"


class GigaProductNotFound(GigaMaterialDownloadError):
    code = "product_not_found"


class ResourcePackageUnavailable(GigaMaterialDownloadError):
    code = "resource_package_unavailable"


class DownloadTimeout(GigaMaterialDownloadError):
    code = "download_timeout"


class DownloadValidationError(GigaMaterialDownloadError):
    code = "invalid_download"


class GigaMaterialDownloaderBusy(GigaMaterialDownloadError):
    code = "download_in_progress"


class WebBridgeClient(Protocol):
    """The small injectable seam used by :class:`GigaMaterialDownloader`."""

    def command(self, action: str, args: dict[str, Any]) -> Any:
        """Run one Kimi WebBridge command."""


class KimiWebBridgeClient:
    """Minimal HTTP client for the current Kimi WebBridge ``{ok, data}`` API."""

    def __init__(
        self,
        daemon_url: str | None = None,
        *,
        session: str = DEFAULT_SESSION,
        timeout: float = 30,
    ) -> None:
        self.daemon_url = (daemon_url or DEFAULT_DAEMON_URL).strip()
        self.session = session
        self.timeout = float(timeout)
        # The WebBridge daemon is loopback-only.  Respecting a corporate HTTP
        # proxy here can route 127.0.0.1 incorrectly or leak local requests.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def command(self, action: str, args: dict[str, Any]) -> Any:
        body = json.dumps(
            {"action": action, "args": args, "session": self.session},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.daemon_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise KimiWebBridgeError(
                "无法连接 Kimi WebBridge，请确认守护进程和浏览器扩展已连接。"
            ) from exc

        if not isinstance(payload, dict) or not payload.get("ok"):
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload.get("message") or "").strip()
            raise KimiWebBridgeError(
                f"Kimi WebBridge 未能完成浏览器操作{f'：{detail}' if detail else ''}。"
            )
        return payload.get("data")


def validate_item_code(value: Any) -> str:
    """Validate and normalize a GIGA Item Code without accepting path-like text."""

    if not isinstance(value, str):
        raise InvalidItemCodeError("请输入有效的 GIGA Item Code。")
    item_code = value.strip()
    if not item_code or item_code != value:
        raise InvalidItemCodeError("Item Code 不能为空，且不能包含首尾空格。")
    if not ITEM_CODE_RE.fullmatch(item_code):
        raise InvalidItemCodeError(
            "Item Code 只能包含英文字母、数字、点、下划线或连字符，最长 80 个字符。"
        )
    return item_code.upper()


def normalize_include_variants(value: Any) -> bool:
    """Accept JSON booleans and a small, explicit set of form-safe values."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise InvalidDownloadOptionError("“下载同链接全部变体”必须为 true 或 false。")


def _safe_name_component(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._-")[:100]
    return cleaned or fallback


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ``evaluate`` returns ``{type: 'string', value: '<json>'}`` in current
# WebBridge releases.  The markers make mocked tests and later debugging less
# brittle, while the DOM selectors are intentionally broad enough for GIGA's
# Chinese and English UI variants.
PAGE_STATE_JS = r"""
(() => {
  /* __GIGA_MATERIAL_PAGE_STATE__ */
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const body = clean(document.body && document.body.innerText);
  const title = clean(document.title);
  const allText = `${location.href} ${title} ${body}`.toLowerCase();
  const codePattern = /(?:item\s*code|itemcode|商品编码|商品編碼|货号|貨號|sku)\s*[:：#]?\s*([a-z0-9][a-z0-9._-]{0,79})\b/i;
  const codeFrom = value => {
    const match = clean(value).match(codePattern);
    return match ? String(match[1]).toUpperCase() : '';
  };
  const directCode = el => {
    if (!el) return '';
    for (const name of ['data-item-code', 'data-itemcode', 'data-sku', 'data-product-code', 'data-code']) {
      const value = clean(el.getAttribute && el.getAttribute(name));
      if (/^[a-z0-9][a-z0-9._-]{0,79}$/i.test(value)) return value.toUpperCase();
    }
    return codeFrom(`${el.textContent || ''} ${el.getAttribute && (el.getAttribute('title') || '')}`);
  };
  const cssPath = element => {
    if (!element || element.nodeType !== 1) return '';
    if (element.id) return `#${CSS.escape(element.id)}`;
    const parts = [];
    let current = element;
    while (current && current.nodeType === 1 && current !== document.body && parts.length < 7) {
      let part = current.tagName.toLowerCase();
      let index = 1;
      let sibling = current;
      while ((sibling = sibling.previousElementSibling)) {
        if (sibling.tagName === current.tagName) index += 1;
      }
      part += `:nth-of-type(${index})`;
      parts.unshift(part);
      current = current.parentElement;
    }
    return parts.length ? `body > ${parts.join(' > ')}` : '';
  };
  const variantControls = [];
  const seenVariants = new Set();
  const addVariant = value => {
    if (!value || !value.kind || !value.selector) return;
    const label = clean(value.label).slice(0, 240);
    if (!label) return;
    const key = `${value.kind}|${value.selector}|${value.value || ''}|${value.href || ''}`;
    if (seenVariants.has(key) || variantControls.length >= 100) return;
    seenVariants.add(key);
    variantControls.push({
      kind: value.kind,
      selector: String(value.selector).slice(0, 1200),
      value: clean(value.value).slice(0, 300),
      label,
      href: clean(value.href).slice(0, 1800),
      item_code: clean(value.item_code).toUpperCase().slice(0, 80),
      index: Number.isInteger(value.index) ? value.index : 0,
    });
  };
  document.querySelectorAll('select').forEach((select, selectIndex) => {
    const options = Array.from(select.options || []).filter(option => !option.disabled && clean(option.textContent));
    if (options.length < 2) return;
    options.forEach((option, optionIndex) => {
      const label = clean(option.textContent);
      if (/^(please select|请选择|select)/i.test(label)) return;
      addVariant({
        kind: 'select', selector: cssPath(select), value: option.value,
        label, item_code: directCode(option) || codeFrom(label), index: selectIndex * 1000 + optionIndex,
      });
    });
  });
  const controlSelector = 'button,a[href],[role="button"],input[type="radio"],label,[data-variant],[data-option-value],[data-item-code],[data-sku]';
  Array.from(document.querySelectorAll(controlSelector)).forEach((el, index) => {
    if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') return;
    const label = clean(el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent || el.value);
    const hint = clean(`${el.className || ''} ${el.id || ''} ${el.getAttribute('data-variant') || ''} ${el.getAttribute('data-option-value') || ''} ${el.getAttribute('data-item-code') || ''} ${el.getAttribute('data-sku') || ''}`);
    const isVariant = /variant|option|swatch|colour|color|size|attribute|specification|sku|product[-_ ]?option|product[-_ ]?variant/i.test(hint);
    if (!isVariant || !label || /resource\s*package|素材包|下载素材|download\s*(?:material|resource)|add\s*to\s*cart|加入购物车/i.test(label)) return;
    const href = clean(el.getAttribute('href'));
    const kind = href && !/^javascript:/i.test(href) ? 'href' : 'click';
    addVariant({kind, selector: cssPath(el), label, href, item_code: directCode(el) || codeFrom(label), index});
  });
  const productLinks = [];
  const seenLinks = new Set();
  Array.from(document.querySelectorAll('a[href]')).forEach(anchor => {
    const href = clean(anchor.href || anchor.getAttribute('href'));
    const label = clean(`${anchor.textContent || ''} ${(anchor.parentElement && anchor.parentElement.textContent) || ''}`);
    const candidate = /route=product\/product|\/product\//i.test(href) || !!directCode(anchor);
    if (!candidate || !href || seenLinks.has(href) || productLinks.length >= 80) return;
    seenLinks.add(href);
    productLinks.push({href, label: label.slice(0, 400), item_code: directCode(anchor) || codeFrom(label)});
  });
  const resourceControls = Array.from(document.querySelectorAll('button,a,[role="button"],span,div'))
    .filter(el => visible(el))
    .filter(el => /resource\s*package|素材包|下载素材|download\s*(?:material|resource)/i.test(clean(el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent)))
    .slice(0, 8);
  const verification = /route=safe\/captcha|safe checker|captcha|security check|robot check|verify you are human|human verification|人机验证|安全验证|安全校验|验证码/.test(allText);
  const login = /route=account\/login|\/login(?:[/?#]|$)|\blog\s*in\b|\bsign\s*in\b|账户登录|帳戶登錄|请登录|請登錄|登录账号|登入帳號/.test(allText);
  return JSON.stringify({
    url: location.href,
    title,
    body_excerpt: body.slice(0, 12000),
    ready: !!document.body && body.length > 0,
    needs_verification: verification,
    needs_login: login,
    item_code: codeFrom(body),
    resource_package: resourceControls.length > 0,
    variant_controls: variantControls,
    product_links: productLinks,
  });
})()
"""


CLICK_RESOURCE_PACKAGE_JS = r"""
(() => {
  /* __GIGA_MATERIAL_CLICK_RESOURCE__ */
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const wanted = /resource\s*package|素材包|下载素材|download\s*(?:material|resource)/i;
  const candidates = Array.from(document.querySelectorAll('button,a,[role="button"],span,div'))
    .filter(el => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
    .filter(el => wanted.test(clean(el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent)));
  const element = candidates.sort((a, b) => clean(a.textContent).length - clean(b.textContent).length)[0];
  if (!element) return JSON.stringify({clicked: false, reason: 'resource_package_not_found'});
  const target = element.closest('button,a,[role="button"]') || element;
  target.click();
  return JSON.stringify({clicked: true, label: clean(target.getAttribute('aria-label') || target.getAttribute('title') || target.textContent).slice(0, 240)});
})()
"""


def _activate_variant_js(target: dict[str, Any]) -> str:
    """Build an escaped page-side interaction for a recorded variant control."""

    encoded = json.dumps(
        {
            "kind": target.get("kind"),
            "selector": target.get("selector"),
            "value": target.get("value"),
        },
        ensure_ascii=False,
    )
    return f"""
(() => {{
  /* __GIGA_MATERIAL_ACTIVATE_VARIANT__ */
  const target = {encoded};
  const element = document.querySelector(target.selector || '');
  if (!element) return JSON.stringify({{clicked: false, reason: 'variant_selector_not_found'}});
  if (element.disabled || element.getAttribute('aria-disabled') === 'true') {{
    return JSON.stringify({{clicked: false, reason: 'variant_disabled'}});
  }}
  if (target.kind === 'select') {{
    const option = Array.from(element.options || []).find(item => String(item.value) === String(target.value));
    if (!option || option.disabled) return JSON.stringify({{clicked: false, reason: 'variant_option_not_found'}});
    element.value = option.value;
    element.dispatchEvent(new Event('input', {{bubbles: true}}));
    element.dispatchEvent(new Event('change', {{bubbles: true}}));
    return JSON.stringify({{clicked: true, kind: 'select'}});
  }}
  const clickable = element.closest('button,a,[role="button"],label') || element;
  clickable.click();
  return JSON.stringify({{clicked: true, kind: 'click'}});
}})()
"""


ProgressCallback = Callable[[dict[str, Any]], None]


class GigaMaterialDownloader:
    """Serial, UI-friendly downloader for all material packages on one GIGA page.

    ``webbridge_client`` may be a :class:`KimiWebBridgeClient`, any object with
    ``command(action, args)``, or a callable accepting ``(action, args)``.  A
    fake client can therefore drive the full workflow in unit tests without a
    browser or a GIGA account.
    """

    def __init__(
        self,
        webbridge_client: WebBridgeClient | Callable[[str, dict[str, Any]], Any] | None = None,
        downloads_dir: str | Path | None = None,
        *,
        page_timeout: float = 20,
        download_timeout: float = 180,
        poll_interval: float = 1,
        stable_polls: int = 2,
        giga_base_url: str | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        run_lock: Any | None = None,
    ) -> None:
        if page_timeout < 0 or download_timeout < 0 or poll_interval < 0:
            raise ValueError("超时和轮询间隔不能为负数")
        if stable_polls < 1:
            raise ValueError("stable_polls 至少为 1")
        self.webbridge_client = webbridge_client or KimiWebBridgeClient()
        self.downloads_dir = Path(downloads_dir or (Path.home() / "Downloads")).expanduser()
        self.page_timeout = float(page_timeout)
        self.download_timeout = float(download_timeout)
        self.poll_interval = float(poll_interval)
        self.stable_polls = int(stable_polls)
        self.giga_base_url = (giga_base_url or DEFAULT_GIGA_BASE_URL).rstrip("/")
        self._clock = clock or time.monotonic
        self._wall_clock_ns: Callable[[], int] = time.time_ns
        self._sleep = sleeper or time.sleep
        self._run_lock = run_lock if run_lock is not None else _GLOBAL_DOWNLOAD_LOCK

        parsed = urlparse(self.giga_base_url)
        base_host = (parsed.hostname or "").casefold()
        self._allowed_hosts = {"gigab2b.com", "www.gigab2b.com"}
        if base_host:
            self._allowed_hosts.add(base_host)

    def download(
        self,
        item_code: str,
        run_dir: str | Path,
        *,
        product_url: str | None = None,
        include_variants: bool = True,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Download each unique variant's Resource Package into ``run_dir``.

        The method returns an entirely JSON-serializable result.  Individual
        variant failures never discard already downloaded packages; a result is
        ``partial`` when at least one package succeeded and at least one did
        not.  Login/CAPTCHA failures are terminal and explicitly marked as
        ``needs_login`` / ``needs_verification``.
        """

        result: dict[str, Any] = {
            "ok": False,
            "status": "queued",
            "message": "素材包下载任务已创建",
            "requested_item_code": str(item_code or ""),
            "include_variants": True,
            "variants": [],
            "errors": [],
            "events": [],
            "downloaded_count": 0,
            "failed_count": 0,
        }

        if not self._run_lock.acquire(blocking=False):
            error = GigaMaterialDownloaderBusy("已有 GIGA 素材下载任务正在运行，请等待其完成。")
            result.update({"status": error.code, "message": str(error), "errors": [error.as_dict()]})
            self._emit(result, progress, "failed", str(error), error=error.as_dict())
            return result

        try:
            normalized_code = validate_item_code(item_code)
            include_variants = normalize_include_variants(include_variants)
            result["requested_item_code"] = normalized_code
            result["include_variants"] = include_variants
            result.update({"status": "running", "message": "正在读取 GIGA 商品页"})
            destination_dir = self._prepare_run_dir(run_dir)
            direct_url = self._validate_product_url(product_url) if product_url else None

            self._emit(result, progress, "resolving", "正在打开并核对 GIGA 商品页", item_code=normalized_code)
            base_state = self._resolve_product_page(normalized_code, direct_url)
            product_page_url = self._state_url(base_state) or direct_url or ""
            if not product_page_url:
                raise GigaProductNotFound("未能读取 GIGA 商品页链接。")

            self._emit(
                result,
                progress,
                "enumerating",
                "正在读取同页变体" if include_variants else "仅下载当前商品素材包",
                item_code=normalized_code,
                include_variants=include_variants,
            )
            targets = self._enumerate_targets(
                base_state,
                product_page_url,
                result,
                progress,
                requested_item_code=normalized_code,
                include_variants=include_variants,
            )
            if not targets and not result["variants"]:
                raise GigaProductNotFound("商品页未找到可下载的变体。")

            total = len(targets)
            for ordinal, target in enumerate(targets, start=1):
                record = target["record"]
                actual_item_code = target["actual_item_code"]
                self._emit(
                    result,
                    progress,
                    "downloading",
                    f"正在下载 {actual_item_code} 的素材包",
                    item_code=normalized_code,
                    actual_item_code=actual_item_code,
                    completed=ordinal - 1,
                    total=total,
                )
                try:
                    downloaded = self._download_target(target, product_page_url, destination_dir)
                    record.update(downloaded)
                    record["status"] = "downloaded"
                    self._emit(
                        result,
                        progress,
                        "downloaded",
                        f"已下载 {actual_item_code} 的素材包",
                        item_code=normalized_code,
                        actual_item_code=actual_item_code,
                        completed=ordinal,
                        total=total,
                        filename=record.get("filename"),
                    )
                except GigaMaterialDownloadError as exc:
                    self._mark_variant_failed(record, exc)
                    self._append_error(result, exc, actual_item_code=actual_item_code)
                    self._emit(
                        result,
                        progress,
                        "variant_failed",
                        f"{actual_item_code} 下载失败：{exc}",
                        item_code=normalized_code,
                        actual_item_code=actual_item_code,
                        completed=ordinal,
                        total=total,
                        error=exc.as_dict(),
                    )
                    # Verification/login checks in a later variant should stop
                    # interaction immediately; files downloaded before it stay
                    # in the run directory and are returned to the caller.
                    if isinstance(exc, (GigaLoginRequired, GigaVerificationRequired)):
                        raise
                except Exception as exc:  # Defensive boundary for one variant.
                    wrapped = GigaMaterialDownloadError(f"下载过程异常：{exc}")
                    self._mark_variant_failed(record, wrapped)
                    self._append_error(result, wrapped, actual_item_code=actual_item_code)
                    self._emit(
                        result,
                        progress,
                        "variant_failed",
                        f"{actual_item_code} 下载失败：{wrapped}",
                        item_code=normalized_code,
                        actual_item_code=actual_item_code,
                        completed=ordinal,
                        total=total,
                        error=wrapped.as_dict(),
                    )

            self._finish_result(result, progress)
            return result
        except GigaMaterialDownloadError as exc:
            result["status"] = exc.code
            result["message"] = str(exc)
            self._append_error(result, exc)
            self._emit(result, progress, "failed", str(exc), error=exc.as_dict())
            self._finish_result(result, progress, terminal_error=exc)
            return result
        except Exception as exc:  # Do not let a browser/OS exception break a job poller.
            wrapped = GigaMaterialDownloadError(f"素材包下载异常：{exc}")
            result["status"] = wrapped.code
            result["message"] = str(wrapped)
            self._append_error(result, wrapped)
            self._emit(result, progress, "failed", str(wrapped), error=wrapped.as_dict())
            self._finish_result(result, progress, terminal_error=wrapped)
            return result
        finally:
            self._run_lock.release()

    # A readable alias for job handlers that describe the user-visible action.
    download_all_variants = download

    def _prepare_run_dir(self, run_dir: str | Path) -> Path:
        if not isinstance(run_dir, (str, Path)) or not str(run_dir).strip():
            raise ValueError("缺少素材下载目录")
        destination = Path(run_dir).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        try:
            resolved = destination.resolve(strict=True)
        except OSError as exc:
            raise ValueError("素材下载目录不可用") from exc
        if not resolved.is_dir():
            raise ValueError("素材下载目录必须是文件夹")
        return resolved

    def _validate_product_url(self, product_url: str | None) -> str:
        value = str(product_url or "").strip()
        parsed = urlparse(value)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme not in {"http", "https"} or host not in self._allowed_hosts:
            raise GigaProductNotFound("商品链接必须是 GIGA 商品页链接。")
        return value

    def _search_url(self, item_code: str) -> str:
        return f"{self.giga_base_url}/index.php?route=product/search&search={quote(item_code, safe='')}"

    def _bridge_command(self, action: str, args: dict[str, Any]) -> Any:
        client = self.webbridge_client
        try:
            if hasattr(client, "command"):
                raw = client.command(action, args)  # type: ignore[union-attr]
            elif callable(client):
                raw = client(action, args)  # type: ignore[misc]
            else:
                raise TypeError("webbridge_client 必须提供 command(action, args)")
        except GigaMaterialDownloadError:
            raise
        except Exception as exc:
            raise KimiWebBridgeError("调用 Kimi WebBridge 失败，请确认浏览器已连接。") from exc

        # Permit both an injected raw daemon response and the data-only result
        # returned by KimiWebBridgeClient.
        if isinstance(raw, dict) and "ok" in raw:
            if not raw.get("ok"):
                detail = str(raw.get("error") or raw.get("message") or "").strip()
                raise KimiWebBridgeError(
                    f"Kimi WebBridge 未能完成浏览器操作{f'：{detail}' if detail else ''}。"
                )
            return raw.get("data")
        if isinstance(raw, dict) and raw.get("success") is False:
            detail = str(raw.get("error") or raw.get("message") or "").strip()
            raise KimiWebBridgeError(
                f"Kimi WebBridge 未能完成浏览器操作{f'：{detail}' if detail else ''}。"
            )
        return raw

    def _navigate(self, url: str) -> None:
        data = self._bridge_command(
            "navigate", {"url": url, "newTab": False, "group_title": "GIGA 素材包下载"}
        )
        if isinstance(data, dict) and data.get("success") is False:
            raise KimiWebBridgeError("Kimi WebBridge 未能打开 GIGA 商品页。")

    def _evaluate(self, code: str) -> dict[str, Any]:
        data = self._bridge_command("evaluate", {"code": code})
        if isinstance(data, dict) and data.get("exceptionDetails"):
            raise KimiWebBridgeError("浏览器页面脚本执行失败，页面可能已变化。")
        value: Any = data
        if isinstance(data, dict):
            if "value" in data:
                value = data.get("value")
            elif isinstance(data.get("result"), dict) and "value" in data["result"]:
                value = data["result"].get("value")
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise KimiWebBridgeError("Kimi WebBridge 返回了无法解析的页面数据。") from exc
            if isinstance(parsed, dict):
                return parsed
        raise KimiWebBridgeError("Kimi WebBridge 返回的页面数据格式无效。")

    def _read_page_state(self) -> dict[str, Any]:
        state = self._evaluate(PAGE_STATE_JS)
        self._raise_if_blocked(state)
        return state

    def _raise_if_blocked(self, state: dict[str, Any]) -> None:
        url = str(state.get("url") or "")
        title = str(state.get("title") or "")
        text = str(state.get("body_excerpt") or "")
        marker = f"{url} {title} {text}".casefold()
        verification = bool(state.get("needs_verification")) or any(
            token in marker
            for token in (
                "route=safe/captcha",
                "safe checker",
                "captcha",
                "security check",
                "robot check",
                "human verification",
                "人机验证",
                "安全验证",
                "安全校验",
            )
        )
        if verification:
            raise GigaVerificationRequired(
                "GIGA 要求安全验证（Safe Checker/CAPTCHA）。请在浏览器中手动完成验证后重新开始；系统不会绕过验证。",
                details={"url": url, "title": title},
            )
        login = bool(state.get("needs_login")) or any(
            token in marker
            for token in ("route=account/login", "登录", "登入", "log in", "sign in")
        )
        if login:
            raise GigaLoginRequired(
                "GIGA 登录已失效，请在浏览器中完成登录后重新开始下载。",
                details={"url": url, "title": title},
            )

    def _wait_for_page_state(self, *, product_page: bool) -> dict[str, Any]:
        deadline = self._clock() + self.page_timeout
        last_state: dict[str, Any] = {}
        first = True
        while first or self._clock() <= deadline:
            first = False
            last_state = self._read_page_state()
            if product_page:
                if bool(last_state.get("resource_package")):
                    return last_state
            elif bool(last_state.get("ready")) or last_state.get("product_links"):
                return last_state
            if self._clock() > deadline:
                break
            self._sleep(self.poll_interval)
        if product_page:
            raise ResourcePackageUnavailable("商品页未找到“下载素材包 / Resource Package”按钮。")
        raise GigaProductNotFound("GIGA 搜索页加载超时，未能读取商品信息。", details={"last_url": self._state_url(last_state)})

    def _resolve_product_page(self, item_code: str, product_url: str | None) -> dict[str, Any]:
        self._navigate(product_url or self._search_url(item_code))
        state = self._wait_for_page_state(product_page=False)
        if state.get("resource_package"):
            return state

        links = state.get("product_links")
        if not isinstance(links, list):
            links = []
        exact: list[dict[str, Any]] = []
        fallback: list[dict[str, Any]] = []
        for raw in links:
            if not isinstance(raw, dict):
                continue
            href = self._same_site_url(raw.get("href"), self._state_url(state))
            if not href:
                continue
            candidate = {"href": href, "item_code": self._maybe_item_code(raw.get("item_code")), "label": str(raw.get("label") or "")}
            if candidate["item_code"] == item_code:
                exact.append(candidate)
            elif item_code.casefold() in candidate["label"].casefold():
                fallback.append(candidate)
        target = (exact or fallback)
        if not target:
            raise GigaProductNotFound(f"未在 GIGA 搜索结果中找到 Item Code：{item_code}。")
        self._navigate(target[0]["href"])
        return self._wait_for_page_state(product_page=True)

    @staticmethod
    def _state_url(state: dict[str, Any]) -> str:
        return str(state.get("url") or "").strip()

    @staticmethod
    def _maybe_item_code(value: Any) -> str:
        try:
            return validate_item_code(value)
        except InvalidItemCodeError:
            return ""

    def _same_site_url(self, value: Any, base_url: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        candidate = urljoin(base_url or self.giga_base_url + "/", raw)
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").casefold() not in self._allowed_hosts:
            return ""
        return candidate

    def _variant_controls(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        raw_controls = state.get("variant_controls")
        if not isinstance(raw_controls, list):
            return []
        controls: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for raw in raw_controls:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "").strip().lower()
            if kind not in {"click", "select", "href"}:
                continue
            selector = str(raw.get("selector") or "").strip()
            label = str(raw.get("label") or "").strip()[:240]
            href = str(raw.get("href") or "").strip()
            value = str(raw.get("value") or "").strip()[:300]
            if not label or (kind != "href" and not selector):
                continue
            key = (kind, selector, value, href)
            if key in seen:
                continue
            seen.add(key)
            controls.append(
                {
                    "kind": kind,
                    "selector": selector[:1200],
                    "label": label,
                    "href": href[:1800],
                    "value": value,
                    "item_code": self._maybe_item_code(raw.get("item_code")),
                }
            )
        return controls

    def _enumerate_targets(
        self,
        base_state: dict[str, Any],
        product_url: str,
        result: dict[str, Any],
        progress: ProgressCallback | None,
        *,
        requested_item_code: str,
        include_variants: bool,
    ) -> list[dict[str, Any]]:
        candidates = [{"kind": "current", "label": "当前商品", "item_code": self._maybe_item_code(base_state.get("item_code"))}]
        if include_variants:
            candidates.extend(self._variant_controls(base_state))

        targets: list[dict[str, Any]] = []
        seen_codes: set[str] = set()
        for index, candidate in enumerate(candidates, start=1):
            label = str(candidate.get("label") or f"变体 {index}")
            record: dict[str, Any] = {
                "label": label,
                "actual_item_code": "",
                "status": "queued",
                "file": "",
                "filename": "",
                "source_filename": "",
                "error": None,
            }
            result["variants"].append(record)
            try:
                state = self._select_target(candidate, product_url)
                actual_item_code = self._maybe_item_code(state.get("item_code")) or candidate.get("item_code") or ""
                actual_item_code = self._maybe_item_code(actual_item_code)
                if not actual_item_code:
                    raise GigaProductNotFound(f"未能读取变体“{label}”的实际 Item Code。")
                record["actual_item_code"] = actual_item_code
                dedupe_key = actual_item_code.casefold()
                if dedupe_key in seen_codes:
                    record.update({"status": "skipped", "note": "与同页已识别的 Item Code 重复，已跳过。"})
                    self._emit(
                        result,
                        progress,
                        "variant_skipped",
                        f"跳过重复变体 {actual_item_code}",
                        item_code=requested_item_code,
                        actual_item_code=actual_item_code,
                    )
                    continue
                seen_codes.add(dedupe_key)
                targets.append({**candidate, "actual_item_code": actual_item_code, "record": record})
                self._emit(
                    result,
                    progress,
                    "variant_found",
                    f"已识别变体 {actual_item_code}",
                    item_code=requested_item_code,
                    actual_item_code=actual_item_code,
                    total=len(candidates),
                )
            except GigaMaterialDownloadError as exc:
                self._mark_variant_failed(record, exc)
                self._append_error(result, exc, label=label)
                self._emit(
                    result,
                    progress,
                    "variant_failed",
                    f"读取变体“{label}”失败：{exc}",
                    item_code=requested_item_code,
                    error=exc.as_dict(),
                )
                if isinstance(exc, (GigaLoginRequired, GigaVerificationRequired)):
                    raise
        return targets

    def _select_target(self, target: dict[str, Any], product_url: str) -> dict[str, Any]:
        # Resetting to the original product page before every control makes
        # sequential runs deterministic and avoids relying on a previous
        # variant's transient DOM state.
        if target.get("kind") == "href":
            variant_url = self._same_site_url(target.get("href"), product_url)
            if not variant_url:
                raise GigaProductNotFound("变体链接不是有效的 GIGA 商品页链接。")
            self._navigate(variant_url)
            return self._wait_for_page_state(product_page=True)

        self._navigate(product_url)
        base_state = self._wait_for_page_state(product_page=True)
        if target.get("kind") == "current":
            return base_state

        action = self._evaluate(_activate_variant_js(target))
        if not action.get("clicked"):
            reason = str(action.get("reason") or "unknown")
            raise GigaProductNotFound(f"无法选择变体：{reason}。")
        return self._wait_for_page_state(product_page=True)

    def _download_target(self, target: dict[str, Any], product_url: str, run_dir: Path) -> dict[str, Any]:
        selected_state = self._select_target(target, product_url)
        actual_item_code = target["actual_item_code"]
        current_code = self._maybe_item_code(selected_state.get("item_code"))
        if current_code and current_code.casefold() != actual_item_code.casefold():
            raise GigaProductNotFound(
                f"变体切换后页面实际 Item Code 为 {current_code}，与预期 {actual_item_code} 不一致。"
            )
        if not selected_state.get("resource_package"):
            raise ResourcePackageUnavailable(f"{actual_item_code} 页面没有可用的素材包下载按钮。")

        before = self._snapshot_downloads()
        started_ns = self._wall_clock_ns()
        click = self._evaluate(CLICK_RESOURCE_PACKAGE_JS)
        if not click.get("clicked"):
            raise ResourcePackageUnavailable("未能点击“下载素材包 / Resource Package”按钮。")
        source = self._wait_for_new_download(before, started_ns)
        destination = self._copy_verified_zip(source, run_dir, actual_item_code)
        return {
            "actual_item_code": actual_item_code,
            "file": destination.name,
            "filename": destination.name,
            "source_filename": source.name,
            "size": destination.stat().st_size,
            "downloaded_at": _utc_now(),
            "error": None,
        }

    def _download_root(self) -> Path:
        try:
            root = self.downloads_dir.resolve(strict=True)
        except OSError as exc:
            raise DownloadTimeout(f"浏览器下载目录不存在或不可访问：{self.downloads_dir}") from exc
        if not root.is_dir():
            raise DownloadTimeout(f"浏览器下载目录不是文件夹：{root}")
        return root

    def _snapshot_downloads(self) -> dict[Path, tuple[int, int]]:
        root = self._download_root()
        snapshot: dict[Path, tuple[int, int]] = {}
        for candidate in root.iterdir():
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
                stat = resolved.stat()
            except (OSError, ValueError):
                continue
            snapshot[resolved] = (stat.st_size, stat.st_mtime_ns)
        return snapshot

    def _wait_for_new_download(self, before: dict[Path, tuple[int, int]], started_ns: int) -> Path:
        deadline = self._clock() + self.download_timeout
        stable: dict[Path, tuple[tuple[int, int], int]] = {}
        rejected: dict[str, str] = {}
        first = True
        while first or self._clock() <= deadline:
            first = False
            root = self._download_root()
            candidates: list[tuple[Path, tuple[int, int]]] = []
            for candidate in root.iterdir():
                try:
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    path = candidate.resolve(strict=True)
                    path.relative_to(root)
                    stat = path.stat()
                except (OSError, ValueError):
                    continue
                fingerprint = (stat.st_size, stat.st_mtime_ns)
                if before.get(path) == fingerprint:
                    rejected[path.name] = "stale"
                    continue
                # A file which predates the click cannot be attributed to this
                # run, even when it appeared after the baseline scan.
                if stat.st_mtime_ns + 1_000_000_000 < started_ns:
                    rejected[path.name] = "stale"
                    continue
                lower_name = path.name.casefold()
                if lower_name.endswith(PARTIAL_DOWNLOAD_SUFFIXES):
                    rejected[path.name] = "partial"
                    continue
                if path.suffix.casefold() != ".zip":
                    rejected[path.name] = "not_zip_extension"
                    continue
                candidates.append((path, fingerprint))

            for path, fingerprint in sorted(candidates, key=lambda row: row[1][1], reverse=True):
                previous = stable.get(path)
                stable_count = previous[1] + 1 if previous and previous[0] == fingerprint else 1
                stable[path] = (fingerprint, stable_count)
                if stable_count < self.stable_polls:
                    continue
                try:
                    self._validate_zip(path)
                except DownloadValidationError as exc:
                    rejected[path.name] = exc.code
                    continue
                return path

            if self._clock() > deadline:
                break
            self._sleep(self.poll_interval)

        details = {"rejected": dict(sorted(rejected.items()))} if rejected else {}
        if any(value in {"partial", "not_zip_extension", "invalid_download"} for value in rejected.values()):
            raise DownloadValidationError("本次下载得到的文件未完成或不是有效 ZIP 素材包。", details=details)
        raise DownloadTimeout("等待本次 GIGA 素材包下载超时，未发现新的有效 ZIP 文件。", details=details)

    @staticmethod
    def _validate_zip(path: Path) -> None:
        try:
            source_size = path.stat().st_size
            if path.suffix.casefold() != ".zip" or source_size <= 0:
                raise DownloadValidationError("下载文件不是有效 ZIP 素材包。")
            if source_size > MAX_DOWNLOAD_ARCHIVE_BYTES:
                raise DownloadValidationError("下载的 ZIP 素材包超过大小上限。")
            if not zipfile.is_zipfile(path):
                raise DownloadValidationError("下载文件不是有效 ZIP 素材包。")
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if not members:
                    raise DownloadValidationError("下载的 ZIP 素材包为空。")
                if len(members) > MAX_DOWNLOAD_ZIP_MEMBERS:
                    raise DownloadValidationError("下载的 ZIP 素材包文件数量超过上限。")
                if sum(max(0, info.file_size) for info in members) > MAX_DOWNLOAD_UNCOMPRESSED_BYTES:
                    raise DownloadValidationError("下载的 ZIP 素材包解压后大小超过上限。")
                for info in members:
                    archive_name = str(info.filename or "").replace("\\", "/")
                    relative = PurePosixPath(archive_name)
                    parts = [part for part in relative.parts if part not in {"", "."}]
                    unix_mode = (info.external_attr >> 16) & 0xFFFF
                    if (
                        not parts
                        or relative.is_absolute()
                        or ".." in parts
                        or re.match(r"^[A-Za-z]:", parts[0])
                        or stat.S_ISLNK(unix_mode)
                        or info.flag_bits & 0x1
                        or info.file_size > MAX_DOWNLOAD_MEMBER_BYTES
                        or (
                            info.file_size
                            and (
                                info.compress_size <= 0
                                or info.file_size / info.compress_size > MAX_DOWNLOAD_COMPRESSION_RATIO
                            )
                        )
                    ):
                        raise DownloadValidationError("下载的 ZIP 素材包未通过安全校验。")
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise DownloadValidationError("下载文件不是可读取的 ZIP 素材包。") from exc

    def _copy_verified_zip(self, source: Path, run_dir: Path, actual_item_code: str) -> Path:
        self._validate_zip(source)
        # Keep the established ``<ItemCode>_image+file*.zip`` convention:
        # process_materials_json.py derives the output folder Item Code from
        # the filename prefix before ``_image``.
        base_name = f"{_safe_name_component(actual_item_code, 'material')}_image+file.zip"
        destination = run_dir / base_name
        suffix = 2
        while destination.exists():
            destination = run_dir / f"{_safe_name_component(actual_item_code, 'material')}_image+file_{suffix}.zip"
            suffix += 1
        # Keep a ``.zip`` suffix while validating the temporary copy.  The
        # browser-facing scanner treats ``.part`` as incomplete, but this file
        # is an internal atomic-copy staging file and must pass ZIP validation.
        temporary = run_dir / f".{destination.stem}.{uuid.uuid4().hex}.tmp.zip"
        try:
            shutil.copyfile(source, temporary)
            self._validate_zip(temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    @staticmethod
    def _mark_variant_failed(record: dict[str, Any], exc: GigaMaterialDownloadError) -> None:
        record.update({"status": "failed", "error": exc.as_dict()})

    @staticmethod
    def _append_error(result: dict[str, Any], exc: GigaMaterialDownloadError, **context: Any) -> None:
        payload = {**exc.as_dict(), **{key: value for key, value in context.items() if value not in (None, "")}}
        errors = result["errors"]
        if not any(
            item.get("code") == payload.get("code")
            and item.get("message") == payload.get("message")
            and all(item.get(key) == value for key, value in context.items() if value not in (None, ""))
            for item in errors
            if isinstance(item, dict)
        ):
            errors.append(payload)

    def _emit(
        self,
        result: dict[str, Any],
        callback: ProgressCallback | None,
        stage: str,
        message: str,
        **extra: Any,
    ) -> None:
        event = {"at": _utc_now(), "stage": stage, "message": message, **extra}
        result["events"].append(event)
        if callback is not None:
            try:
                downloaded = sum(item.get("status") == "downloaded" for item in result["variants"])
                failed = sum(item.get("status") == "failed" for item in result["variants"])
                # The callback payload is safe for a public status endpoint:
                # no browser URL, run-directory path, or absolute file path.
                variants = []
                for item in result["variants"]:
                    snapshot = {
                        key: item.get(key)
                        for key in (
                            "label", "actual_item_code", "status", "file", "filename",
                            "source_filename", "size", "downloaded_at", "note", "error",
                        )
                        if key in item
                    }
                    if isinstance(snapshot.get("error"), dict):
                        snapshot["error"] = dict(snapshot["error"])
                    variants.append(snapshot)
                callback({
                    **event,
                    "status": result.get("status"),
                    "downloaded_count": downloaded,
                    "failed_count": failed,
                    "variants": variants,
                })
            except Exception:
                # Status updates are observability only.  A disconnected HTTP
                # client must not abort a material package already downloading.
                pass

    def _finish_result(
        self,
        result: dict[str, Any],
        progress: ProgressCallback | None,
        *,
        terminal_error: GigaMaterialDownloadError | None = None,
    ) -> None:
        variants = result["variants"]
        downloaded = sum(item.get("status") == "downloaded" for item in variants)
        failed = sum(item.get("status") == "failed" for item in variants)
        result["downloaded_count"] = downloaded
        result["failed_count"] = failed
        if terminal_error is not None:
            # A verification/login failure always remains visible even if an
            # earlier variant already downloaded successfully.
            result["status"] = terminal_error.code
            result["ok"] = False
            result["message"] = str(terminal_error)
            return
        if downloaded and failed:
            result.update({"ok": True, "status": "partial", "message": "素材包部分下载完成"})
            self._emit(result, progress, "completed", "素材包部分下载完成", downloaded=downloaded, failed=failed)
        elif downloaded:
            result.update({"ok": True, "status": "completed", "message": "素材包下载完成"})
            self._emit(result, progress, "completed", "素材包下载完成", downloaded=downloaded, failed=failed)
        else:
            result.update({"ok": False, "status": "failed", "message": "没有成功下载任何素材包"})
            self._emit(result, progress, "failed", "没有成功下载任何素材包", downloaded=downloaded, failed=failed)


def download_giga_material_packages(
    item_code: str,
    run_dir: str | Path,
    *,
    product_url: str | None = None,
    include_variants: bool = True,
    progress: ProgressCallback | None = None,
    **downloader_kwargs: Any,
) -> dict[str, Any]:
    """Convenience wrapper for one-off job handlers."""

    return GigaMaterialDownloader(**downloader_kwargs).download(
        item_code,
        run_dir,
        product_url=product_url,
        include_variants=include_variants,
        progress=progress,
    )
