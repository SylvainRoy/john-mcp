from __future__ import annotations

import json
import subprocess
import sys
from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from john_agent import pdf_text
from john_agent.pdf_text import PdfTextError, extract_pdf_pages


def make_pdf(pages: list[str | None]) -> bytes:
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'),
            NameObject('/BaseFont'): NameObject('/Helvetica'),
        })
        page[NameObject('/Resources')] = DictionaryObject(
            {NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        if text is not None:
            safe = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            content = DecodedStreamObject()
            content.set_data(f'BT /F1 24 Tf 100 700 Td ({safe}) Tj ET'.encode())
            page[NameObject('/Contents')] = content
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_extracts_two_page_text():
    data = make_pdf(['Hello page one', 'Second page text'])
    pages = extract_pdf_pages(data)
    assert len(pages) == 2
    assert 'Hello page one' in pages[0]
    assert 'Second page text' in pages[1]


def test_blank_page_extracts_empty_string():
    pages = extract_pdf_pages(make_pdf(['text', None]))
    assert 'text' in pages[0]
    assert pages[1].strip() == ''


def test_encrypted_pdf_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt('pw')
    buf = BytesIO()
    writer.write(buf)
    with pytest.raises(PdfTextError) as exc:
        extract_pdf_pages(buf.getvalue())
    assert exc.value.code == 'password_required'


def test_malformed_pdf_rejected():
    with pytest.raises(PdfTextError) as exc:
        extract_pdf_pages(b'%PDF-1.4 total garbage not a pdf')
    assert exc.value.code == 'invalid_pdf'


def test_over_100_pages_rejected():
    data = make_pdf(['x'] * 101)
    with pytest.raises(PdfTextError) as exc:
        extract_pdf_pages(data)
    assert exc.value.code == 'too_large'


def test_over_byte_limit_rejected():
    data = b'%PDF' + b'0' * (10 * 1024 * 1024 + 100)
    with pytest.raises(PdfTextError) as exc:
        extract_pdf_pages(data)
    assert exc.value.code == 'too_large'


def test_timeout_maps_to_extraction_timeout(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd='x', timeout=15)

    monkeypatch.setattr(pdf_text.subprocess, 'run', fake_run)
    with pytest.raises(PdfTextError) as exc:
        extract_pdf_pages(b'%PDF-fake')
    assert exc.value.code == 'extraction_timeout'


def test_nonzero_exit_maps_invalid_pdf(monkeypatch):
    class Done:
        returncode = 1
        stdout = b''

    monkeypatch.setattr(pdf_text.subprocess, 'run', lambda *a, **k: Done())
    with pytest.raises(PdfTextError) as exc:
        extract_pdf_pages(b'%PDF-fake')
    assert exc.value.code == 'invalid_pdf'


def test_bad_worker_json_maps_invalid_pdf(monkeypatch):
    class Done:
        returncode = 0
        stdout = b'garbage output SECRET_SENTINEL'

    monkeypatch.setattr(pdf_text.subprocess, 'run', lambda *a, **k: Done())
    with pytest.raises(PdfTextError) as exc:
        extract_pdf_pages(b'%PDF-fake')
    assert exc.value.code == 'invalid_pdf'
    assert 'SECRET_SENTINEL' not in str(exc.value)


def test_worker_env_excludes_credentials_and_api_keys(monkeypatch):
    captured = {}

    class Done:
        returncode = 0
        stdout = json.dumps({'pages': ['ok']}).encode()

    def fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        captured.update(kwargs)
        return Done()

    monkeypatch.setenv('JOHN_GMAIL_CREDENTIALS', 'synthetic-secret')
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-key')
    monkeypatch.setattr(pdf_text.subprocess, 'run', fake_run)
    assert extract_pdf_pages(b'data') == ['ok']
    env = captured['env']
    assert 'JOHN_GMAIL_CREDENTIALS' not in env
    assert 'GEMINI_API_KEY' not in env
    assert env['PYTHONDONTWRITEBYTECODE'] == '1'
    assert captured['stderr'] == subprocess.DEVNULL
    assert captured['timeout'] == 15
    assert '-I' in captured['cmd']
    assert '-B' in captured['cmd']
    assert captured['cmd'][0] == sys.executable


def test_oversize_input_rejected_before_spawn(monkeypatch):
    def fake_run(*a, **k):
        raise AssertionError('worker spawned for oversize input')

    monkeypatch.setattr(pdf_text.subprocess, 'run', fake_run)
    with pytest.raises(PdfTextError) as exc:
        extract_pdf_pages(b'x' * (10 * 1024 * 1024 + 1))
    assert exc.value.code == 'too_large'


def test_malformed_error_field_maps_invalid_pdf(monkeypatch):
    class Done:
        returncode = 0
        stdout = json.dumps({'error': ['not', 'a', 'string']}).encode()

    monkeypatch.setattr(pdf_text.subprocess, 'run', lambda *a, **k: Done())
    with pytest.raises(PdfTextError) as exc:
        extract_pdf_pages(b'%PDF-fake')
    assert exc.value.code == 'invalid_pdf'
