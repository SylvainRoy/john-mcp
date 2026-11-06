"""Bounded PDF text extraction in a fresh subprocess worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from io import BytesIO

_MAX_BYTES = 10 * 1024 * 1024
_MAX_PAGES = 100
_MAX_CHARS = 1_000_000
_MAX_STDOUT = 8 * 1024 * 1024
_TIMEOUT = 15
_WORKER_ENV_KEYS = ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'WINDIR')
_VALID_CODES = {'invalid_pdf', 'password_required', 'no_extractable_text', 'too_large'}


class PdfTextError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _worker_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _WORKER_ENV_KEYS if k in os.environ}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONNOUSERSITE'] = '1'
    return env


def extract_pdf_pages(data: bytes) -> list[str]:
    if len(data) > _MAX_BYTES:
        raise PdfTextError('too_large')
    try:
        proc = subprocess.run(
            [sys.executable, '-I', '-B', '-m', 'john_agent.pdf_text'],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_TIMEOUT,
            check=False,
            env=_worker_env(),
        )
    except subprocess.TimeoutExpired:
        raise PdfTextError('extraction_timeout') from None
    if proc.returncode != 0 or len(proc.stdout or b'') > _MAX_STDOUT:
        raise PdfTextError('invalid_pdf')
    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        raise PdfTextError('invalid_pdf') from None
    if not isinstance(payload, dict):
        raise PdfTextError('invalid_pdf')
    if 'error' in payload:
        code = payload['error']
        if isinstance(code, str) and code in _VALID_CODES:
            raise PdfTextError(code)
        raise PdfTextError('invalid_pdf')
    pages = payload.get('pages')
    if not isinstance(pages, list) or len(pages) > _MAX_PAGES:
        raise PdfTextError('invalid_pdf')
    total = 0
    for page in pages:
        if not isinstance(page, str):
            raise PdfTextError('invalid_pdf')
        total += len(page)
        if total > _MAX_CHARS:
            raise PdfTextError('invalid_pdf')
    return pages


def main() -> None:
    try:
        import platform
        import resource

        if platform.system() == 'Linux':
            try:
                resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
            except (ValueError, OSError):
                pass
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (10,) * 2)
        except (ValueError, OSError):
            pass
    except ImportError:
        pass

    raw = sys.stdin.buffer.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        _emit({'error': 'too_large'})
        return
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw))
        if reader.is_encrypted:
            _emit({'error': 'password_required'})
            return
        count = len(reader.pages)
        if count > _MAX_PAGES:
            _emit({'error': 'too_large'})
            return
        pages = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ''
            text = text.replace('\r\n', '\n').replace('\r', '\n')
            total += len(text)
            if total > _MAX_CHARS:
                _emit({'error': 'too_large'})
                return
            pages.append(text)
        _emit({'pages': pages})
    except Exception:
        _emit({'error': 'invalid_pdf'})


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload))


if __name__ == '__main__':
    main()
