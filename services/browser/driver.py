"""Browser driver seam and the HTTP driver.

Two implementations sit behind one protocol:

* :class:`HttpFormDriver` — parses and submits forms over plain HTTP. No browser, so
  it runs anywhere and makes the runner's whole state machine testable, including
  every safety stop. This is what the tests use.
* ``PlaywrightDriver`` (``playwright_driver.py``) — a real browser for sites that
  need JavaScript to render their form.

Note on this host: Playwright's bundled driver requires GLIBC >= 2.28 and this
machine has 2.27, so the real browser must run in the Docker image
(``mcr.microsoft.com/playwright/python``). The seam is exactly why that is a
deployment detail rather than a rewrite — the runner does not know or care which
driver it holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from packages.core.errors import DomainError
from packages.core.logging import get_logger
from packages.schemas.application import FormField, FormSpec
from services.browser import safety

logger = get_logger(__name__)

_INPUT_RE = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.I)
_ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
_LABEL_RE = re.compile(r"<label\b([^>]*)>(.*?)</label>", re.I | re.S)
_OPTION_RE = re.compile(r"<option\b([^>]*)>(.*?)</option>", re.I | re.S)
_SELECT_BLOCK_RE = re.compile(r"<select\b([^>]*)>(.*?)</select>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)


@dataclass(slots=True)
class PageState:
    """What the driver can see right now."""

    url: str
    title: str = ""
    html: str = ""
    status_code: int = 200

    def verdict(self) -> safety.SafetyVerdict:
        return safety.assess(self.html, url=self.url)


class BrowserDriver(Protocol):
    """The seam the runner drives."""

    name: str

    def open(self, url: str) -> PageState: ...

    def discover_form(self) -> FormSpec: ...

    def fill(self, field_name: str, value: str) -> None: ...

    def attach_file(self, field_name: str, path: Path) -> None: ...

    def submit(self) -> PageState: ...

    def snapshot(self, label: str) -> str | None: ...

    def close(self) -> None: ...


def _attributes(fragment: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _ATTR_RE.finditer(fragment):
        value = match.group(2).strip("\"'")
        found[match.group(1).lower()] = value
    return found


def _labels(html: str) -> dict[str, str]:
    """Map field id -> label text, so mapping can use semantic labels."""
    mapping: dict[str, str] = {}
    for attrs_fragment, inner in _LABEL_RE.findall(html):
        attrs = _attributes(attrs_fragment)
        text = _TAG_RE.sub(" ", inner)
        text = re.sub(r"\s+", " ", text).strip().rstrip("*").strip()
        target = attrs.get("for")
        if target and text:
            mapping[target] = text
    return mapping


def parse_form(html: str) -> FormSpec:
    """Extract a :class:`FormSpec` from HTML.

    Uses names, types and ``<label for=...>`` — the stable, semantic handles
    skills/07 asks for. No coordinates, no positional guessing.
    """
    form_match = _FORM_RE.search(html)
    body = form_match.group(2) if form_match else html
    form_attrs = _attributes(form_match.group(1)) if form_match else {}

    labels = _labels(html)
    select_options: dict[str, list[str]] = {}
    for attrs_fragment, inner in _SELECT_BLOCK_RE.findall(body):
        attrs = _attributes(attrs_fragment)
        name = attrs.get("name") or attrs.get("id") or ""
        options = []
        for option_attrs, option_text in _OPTION_RE.findall(inner):
            value = _attributes(option_attrs).get("value")
            resolved = value if value is not None else re.sub(r"\s+", " ", option_text).strip()
            if resolved:
                options.append(resolved)
        if name:
            select_options[name] = options

    fields: list[FormField] = []
    seen: set[str] = set()

    for tag, attrs_fragment in _INPUT_RE.findall(body):
        attrs = _attributes(attrs_fragment)
        name = attrs.get("name") or attrs.get("id") or ""
        if not name or name in seen:
            continue
        raw_type = (attrs.get("type") or "").lower()
        if raw_type in ("hidden", "submit", "button", "reset", "image"):
            continue
        seen.add(name)

        field_type: str
        if tag.lower() == "textarea":
            field_type = "textarea"
        elif tag.lower() == "select":
            field_type = "select"
        else:
            field_type = raw_type or "text"

        max_length = attrs.get("maxlength")
        fields.append(
            FormField(
                name=name,
                label=labels.get(attrs.get("id", ""), ""),
                field_type=field_type,
                required="required" in attrs_fragment.lower(),
                options=select_options.get(name, []),
                max_length=int(max_length) if max_length and max_length.isdigit() else None,
            )
        )

    return FormSpec(
        url=form_attrs.get("action", ""),
        fields=fields,
        submit_selector="#submit-application",
    )


def _hidden_fields(html: str) -> dict[str, str]:
    """Hidden inputs must be echoed back or a form token is lost."""
    form_match = _FORM_RE.search(html)
    body = form_match.group(2) if form_match else html
    values: dict[str, str] = {}
    for tag, attrs_fragment in _INPUT_RE.findall(body):
        if tag.lower() != "input":
            continue
        attrs = _attributes(attrs_fragment)
        if (attrs.get("type") or "").lower() == "hidden" and attrs.get("name"):
            values[attrs["name"]] = attrs.get("value", "")
    return values


class HttpFormDriver:
    """Drives a plain HTML form over HTTP.

    Deliberately minimal: it follows links, reads forms, fills values and posts. It
    does not execute JavaScript, so it is honest about its limits — a site that needs
    JS gets the Playwright driver instead.
    """

    name = "http"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        snapshot_dir: Path | None = None,
    ) -> None:
        self._client = client or httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "career-agent/0.1 (local, personal use)"},
        )
        self._snapshot_dir = snapshot_dir
        self._state: PageState | None = None
        self._values: dict[str, str] = {}
        self._files: dict[str, Path] = {}

    # ----------------------------------------------------------------- browsing

    def open(self, url: str) -> PageState:
        response = self._client.get(url)
        self._state = PageState(
            url=str(response.url),
            title=self._title(response.text),
            html=response.text,
            status_code=response.status_code,
        )
        self._values = _hidden_fields(response.text)
        self._files.clear()
        return self._state

    @staticmethod
    def _title(html: str) -> str:
        found = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        return re.sub(r"\s+", " ", found.group(1)).strip() if found else ""

    def _require_state(self) -> PageState:
        if self._state is None:
            raise DomainError("no page is open; call open() first")
        return self._state

    def discover_form(self) -> FormSpec:
        return parse_form(self._require_state().html)

    def fill(self, field_name: str, value: str) -> None:
        self._values[field_name] = value

    def attach_file(self, field_name: str, path: Path) -> None:
        if not path.is_file():
            raise DomainError(f"attachment not found: {path}")
        self._files[field_name] = path

    def submit(self) -> PageState:
        state = self._require_state()
        action = self._form_action(state)

        files = {
            name: (path.name, path.read_bytes(), "application/pdf")
            for name, path in self._files.items()
        }
        response = self._client.post(
            action,
            data=self._values,
            files=files or None,
        )
        self._state = PageState(
            url=str(response.url),
            title=self._title(response.text),
            html=response.text,
            status_code=response.status_code,
        )
        return self._state

    def _form_action(self, state: PageState) -> str:
        match = _FORM_RE.search(state.html)
        action = _attributes(match.group(1)).get("action", "") if match else ""
        if not action:
            return state.url
        return str(httpx.URL(state.url).join(action))

    def snapshot(self, label: str) -> str | None:
        """Persist a redacted page snapshot for diagnosis."""
        if self._snapshot_dir is None or self._state is None:
            return None
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = self._snapshot_dir / f"{label}.html"
        path.write_text(safety.redact_html(self._state.html, limit=200_000), encoding="utf-8")
        return str(path)

    def close(self) -> None:
        self._client.close()
