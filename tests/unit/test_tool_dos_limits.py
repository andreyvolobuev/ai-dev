"""DoS-protection limits on user-supplied tool inputs.

A hostile ticket could attach a 5000-page PDF, a 100 MB XLSX
zip-bomb, an HTML page with a million <img> tags, or commit a
symlink in the repo to ``/etc/passwd`` — in every case the tool used
to happily process the input and either burn token budget, OOM the
worker, or read out of the workspace. These tests pin the caps.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter

from virtual_dev.tools import fetch_url, read_pdf_url, read_xlsx_url
from virtual_dev.tools.read_file import run as read_file_run

# ---------- PDF page cap ------------------------------------------------


def _make_pdf(n_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_read_pdf_url_rejects_too_many_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf_bytes = _make_pdf(read_pdf_url.MAX_PAGES + 1)
    monkeypatch.setattr(
        "virtual_dev.tools.read_pdf_url.download_url_bytes",
        lambda url, settings: pdf_bytes,
    )

    result = await read_pdf_url.run(object(), {"url": "https://x/y.pdf"})
    assert result.get("is_error") is True
    text = result["content"][0]["text"]
    assert "page" in text.lower()  # error mentions pages
    assert "limit" in text.lower() or "too" in text.lower()


@pytest.mark.asyncio
async def test_read_pdf_url_accepts_pdf_within_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf_bytes = _make_pdf(3)
    monkeypatch.setattr(
        "virtual_dev.tools.read_pdf_url.download_url_bytes",
        lambda url, settings: pdf_bytes,
    )

    result = await read_pdf_url.run(object(), {"url": "https://x/y.pdf"})
    assert result.get("is_error") is not True
    assert "PDF" in result["content"][0]["text"]


# ---------- XLSX body-size cap -----------------------------------------


@pytest.mark.asyncio
async def test_read_xlsx_url_rejects_oversize_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Body cap fires BEFORE openpyxl attempts to unzip — that's the
    whole point: zip-bomb defence."""
    huge = b"PK\x03\x04" + b"\x00" * (read_xlsx_url.MAX_XLSX_BYTES + 1)
    monkeypatch.setattr(
        "virtual_dev.tools.read_xlsx_url.download_url_bytes",
        lambda url, settings: huge,
    )

    result = await read_xlsx_url.run(object(), {"url": "https://x/y.xlsx"})
    assert result.get("is_error") is True
    text = result["content"][0]["text"].lower()
    assert "byte" in text or "size" in text or "too large" in text


# ---------- Attachment-link cap in fetch_url ---------------------------


def test_extract_attachment_links_caps_output() -> None:
    """A page with 1000 image tags must not flood the prompt."""
    imgs = "".join(
        f'<img src="https://example.com/img{i}.png">' for i in range(1000)
    )
    html = f"<html><body>{imgs}</body></html>"
    links = fetch_url._extract_attachment_links(html, "https://example.com/")
    assert len(links) <= fetch_url.MAX_ATTACHMENT_LINKS


# ---------- read_file symlink reject -----------------------------------


class _FakeFilter:
    def wrap(self, text: str, *, source: str) -> Any:
        class _W:
            wrapped_text = text
        return _W()


class _FakeResearcher:
    DEFAULT_MAX_FILE_BYTES = 12_000

    def __init__(self, repos: dict[str, Any]) -> None:
        self.repos = repos
        self.filter = _FakeFilter()


@pytest.mark.asyncio
async def test_read_file_rejects_symlink_inside_repo(tmp_path: Path) -> None:
    """A symlink within the repo can point outside via a relative path —
    ``relative_to`` resolves it through the link first, so the guard
    needs to refuse symlinks outright."""
    repo = tmp_path / "repo"
    repo.mkdir()
    real_outside = tmp_path / "outside.txt"
    real_outside.write_text("SECRET")
    link = repo / "link.txt"
    link.symlink_to(real_outside)

    from virtual_dev.application.services.researcher import RepoHandle
    researcher = _FakeResearcher(
        repos={"r": RepoHandle(key="r", local_path=repo)},
    )

    result = await read_file_run(researcher, {"path": "link.txt", "repo_key": "r"})
    assert result.get("is_error") is True
    assert "symlink" in result["content"][0]["text"].lower() or \
        "escape" in result["content"][0]["text"].lower()
    # And SECRET must not have leaked.
    assert "SECRET" not in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_read_file_accepts_regular_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.txt").write_text("hello world")

    from virtual_dev.application.services.researcher import RepoHandle
    researcher = _FakeResearcher(
        repos={"r": RepoHandle(key="r", local_path=repo)},
    )
    result = await read_file_run(researcher, {"path": "ok.txt", "repo_key": "r"})
    assert result.get("is_error") is not True
    assert "hello world" in result["content"][0]["text"]
