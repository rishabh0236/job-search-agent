"""Playwright driver for sites whose form needs JavaScript.

Follows skills/07: an isolated browser context per run, semantic selectors, a
screenshot plus page snapshot on failure, and no coordinate automation. Passwords,
cookies and tokens are never logged, and a persistent profile is only ever used when
the user has explicitly authenticated themselves.

Availability: Playwright's bundled driver needs GLIBC >= 2.28. On this host (2.27) it
cannot start, which is why the runner is written against a driver protocol and tested
with the HTTP driver — run this one inside the official
``mcr.microsoft.com/playwright/python`` image, where the runner code is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from packages.core.errors import DomainError
from packages.core.logging import get_logger
from packages.schemas.application import FormSpec
from services.browser import safety
from services.browser.driver import PageState, parse_form

logger = get_logger(__name__)


def playwright_available() -> tuple[bool, str]:
    """Report whether a real browser can actually start here."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        return False, f"playwright is not installed: {exc}"

    try:
        with sync_playwright() as engine:
            _ = engine.chromium.executable_path
        return True, "playwright driver is usable"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"


class PlaywrightDriver:
    """A real browser, one isolated context per run."""

    name = "playwright"

    def __init__(
        self,
        *,
        headless: bool = True,
        snapshot_dir: Path | None = None,
        storage_state: Path | None = None,
        timeout_ms: int = 20_000,
    ) -> None:
        self._headless = headless
        self._snapshot_dir = snapshot_dir
        self._storage_state = storage_state
        self._timeout_ms = timeout_ms
        self._engine: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise DomainError("playwright is not installed") from exc

        self._engine = sync_playwright().start()
        self._browser = self._engine.chromium.launch(headless=self._headless)
        # A fresh context unless the user authenticated deliberately: no ambient
        # session, and no cookies carried between unrelated runs.
        self._context = self._browser.new_context(
            storage_state=str(self._storage_state)
            if self._storage_state and self._storage_state.is_file()
            else None
        )
        self._context.set_default_timeout(self._timeout_ms)
        self._page = self._context.new_page()
        return self._page

    def open(self, url: str) -> PageState:
        page = self._ensure_page()
        response = page.goto(url, wait_until="domcontentloaded")
        return PageState(
            url=page.url,
            title=page.title(),
            html=page.content(),
            status_code=response.status if response else 200,
        )

    def discover_form(self) -> FormSpec:
        return parse_form(self._ensure_page().content())

    def fill(self, field_name: str, value: str) -> None:
        page = self._ensure_page()
        # Stable selectors only, in order of reliability. No coordinates.
        for selector in (f"#{field_name}", f"[name='{field_name}']"):
            locator = page.locator(selector)
            if locator.count() == 0:
                continue
            tag = locator.first.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                locator.first.select_option(value)
            elif locator.first.get_attribute("type") == "checkbox":
                if value.lower() in ("yes", "true", "on", "1"):
                    locator.first.check()
            else:
                locator.first.fill(value)
            return
        raise DomainError(f"no field matching {field_name!r} on the page")

    def attach_file(self, field_name: str, path: Path) -> None:
        page = self._ensure_page()
        page.locator(f"#{field_name}, [name='{field_name}']").first.set_input_files(str(path))

    def submit(self) -> PageState:
        page = self._ensure_page()
        try:
            page.locator("#submit-application, button[type=submit]").first.click()
            page.wait_for_load_state("domcontentloaded")
        except Exception as exc:
            self.snapshot("submit-failure")
            raise DomainError(f"submission click failed: {type(exc).__name__}") from exc

        return PageState(url=page.url, title=page.title(), html=page.content())

    def snapshot(self, label: str) -> str | None:
        if self._snapshot_dir is None or self._page is None:
            return None
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)

        html_path = self._snapshot_dir / f"{label}.html"
        html_path.write_text(
            safety.redact_html(self._page.content(), limit=200_000), encoding="utf-8"
        )
        try:
            self._page.screenshot(path=str(self._snapshot_dir / f"{label}.png"), full_page=True)
        except Exception:
            logger.warning("browser.screenshot_failed", extra={"label": label})
        return str(html_path)

    def close(self) -> None:
        for resource in (self._context, self._browser):
            try:
                if resource is not None:
                    resource.close()
            except Exception:  # noqa: S110 - closing must never raise
                pass
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:  # noqa: S110
                pass
        self._page = self._context = self._browser = self._engine = None
