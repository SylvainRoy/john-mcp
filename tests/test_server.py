from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from mdtodb import CorpusReader

from john_agent.gmail import GmailReader
from john_agent.server import REVIEW_TEMPLATE, create_server


def make_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    md_dir = tmp_path / "md"
    md_dir.mkdir()
    manifest = {"version": 2, "files": {}}
    for rel, text in files.items():
        out = rel.rsplit(".", 1)[0] + ".md"
        target = md_dir / out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
        manifest["files"][rel] = {"fingerprint": f"fp:{rel}", "engine": "fake", "output": out}
    (md_dir / ".pdftomd-manifest.json").write_text(json.dumps(manifest), "utf-8")
    return md_dir


FILES = {
    "personnes/Alice/assurance/contrat-habitation.pdf": "# Assurance\n\nFranchise élevée.\n",
    "banque/comptes/contrat-compte.pdf": "# Compte\n\nFrais de tenue.\n",
}


@pytest.fixture
def md_dir(tmp_path: Path) -> Path:
    return make_tree(tmp_path, FILES)


@pytest.fixture
def reader(md_dir: Path) -> CorpusReader:
    return CorpusReader(md_dir)


def run(coro):
    return asyncio.run(coro)


def test_exactly_five_tools(reader):
    async def go():
        async with create_connected_server_and_client_session(create_server(reader)) as session:
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            assert names == [
                "corpus_status",
                "find_in_document",
                "list_documents",
                "read_document",
                "search_documents",
            ]
            for t in tools.tools:
                assert t.annotations.readOnlyHint is True
                assert t.annotations.destructiveHint is False
                assert t.annotations.idempotentHint is True
                assert t.annotations.openWorldHint is (t.name == "search_documents")
                assert t.description
            search = next(t for t in tools.tools if t.name == "search_documents")
            props = search.inputSchema["properties"]
            assert props["mode"]["enum"] == ["lexical", "semantic", "hybrid"]
            assert set(props) == {"query", "mode", "person", "keywords", "filetype", "limit"}
            read = next(t for t in tools.tools if t.name == "read_document")
            assert set(read.inputSchema["properties"]) == {"document_id", "offset", "max_chars"}
            status = next(t for t in tools.tools if t.name == "corpus_status")
            assert status.inputSchema["properties"] in ({}, None)

    run(go())


def test_tool_calls_and_structured_content(reader):
    async def go():
        async with create_connected_server_and_client_session(create_server(reader)) as session:
            status = await session.call_tool("corpus_status", {})
            assert status.isError is not True
            data = status.structuredContent
            assert data["manifest_documents"] == 2
            assert data["index_status"] == "not_configured"
            assert "Franchise" not in json.dumps(data)

            listed = await session.call_tool("list_documents", {"limit": 1})
            assert listed.structuredContent["total"] == 2
            assert listed.structuredContent["next_offset"] == 1

            found = await session.call_tool(
                "search_documents", {"query": "franchise", "mode": "lexical"}
            )
            results = found.structuredContent["results"]
            assert [r["document"]["document_id"] for r in results] == [
                "personnes/Alice/assurance/contrat-habitation.pdf"
            ]

            read = await session.call_tool(
                "read_document",
                {"document_id": "banque/comptes/contrat-compte.pdf", "max_chars": 10},
            )
            excerpt = read.structuredContent["excerpt"]
            assert excerpt["text"] == FILES["banque/comptes/contrat-compte.pdf"][:10]
            assert len(excerpt["markdown_sha256"]) == 64

            find = await session.call_tool(
                "find_in_document",
                {"document_id": "banque/comptes/contrat-compte.pdf", "query": "FRAIS"},
            )
            assert len(find.structuredContent["matches"]) == 1

    run(go())


def test_tool_error_is_safe(reader):
    async def go():
        async with create_connected_server_and_client_session(create_server(reader)) as session:
            res = await session.call_tool("read_document", {"document_id": "../escape.pdf"})
            assert res.isError is True
            assert "escape" not in str(res.content) or "Invalid" in str(res.content)
            res = await session.call_tool("search_documents", {"query": "x", "mode": "bogus"})
            assert res.isError is True

    run(go())


def test_unexpected_error_sanitized(md_dir: Path, caplog):
    class BoomReader(CorpusReader):
        def corpus_status(self):
            raise RuntimeError("sk-SENTINEL raw detail")

    async def go():
        async with create_connected_server_and_client_session(
            create_server(BoomReader(md_dir))
        ) as session:
            res = await session.call_tool("corpus_status", {})
            assert res.isError is True
            assert "SENTINEL" not in str(res.content)

    with caplog.at_level("ERROR"):
        run(go())
    assert "SENTINEL" not in caplog.text


def test_prompt_exact_content(reader):
    async def go():
        async with create_connected_server_and_client_session(create_server(reader)) as session:
            prompts = await session.list_prompts()
            assert [p.name for p in prompts.prompts] == ["review_opportunities"]
            result = await session.get_prompt("review_opportunities", {"focus": "assurance auto"})
            assert result.description
            text = result.messages[0].content.text
            assert text == REVIEW_TEMPLATE.format(focus="assurance auto")
            default = await session.get_prompt("review_opportunities", {})
            assert default.messages[0].content.text == REVIEW_TEMPLATE.format(
                focus="banking, loans, and insurance"
            )

    run(go())


def test_gmail_tools_are_opt_in_and_lazy(reader):
    class LazyGmail:
        def close(self):
            pass
        def __getattr__(self, name):
            raise AssertionError('Gmail accessed during tool discovery')
    async def go():
        async with create_connected_server_and_client_session(
            create_server(reader, gmail_reader=LazyGmail())
        ) as session:
            tools = (await session.list_tools()).tools
            assert {t.name for t in tools} == {
                'corpus_status', 'list_documents', 'search_documents', 'read_document',
                'find_in_document', 'search_emails', 'read_email', 'read_email_attachment',
            }
            for tool in tools:
                if tool.name in {'search_emails', 'read_email', 'read_email_attachment'}:
                    assert tool.annotations.readOnlyHint is True
                    assert tool.annotations.destructiveHint is False
                    assert tool.annotations.idempotentHint is True
                    assert tool.annotations.openWorldHint is True
            by_name = {t.name: t for t in tools}
            search = by_name['search_emails'].inputSchema['properties']
            assert set(search) == {'query', 'limit', 'page_token', 'include_spam_trash'}
            assert search['limit']['default'] == 10
            assert search['include_spam_trash']['default'] is False
            read = by_name['read_email'].inputSchema['properties']
            assert set(read) == {'message_id', 'offset', 'max_chars'}
            assert read['offset']['default'] == 0
            assert read['max_chars']['default'] == 12000
            att = by_name['read_email_attachment'].inputSchema['properties']
            assert set(att) == {'message_id', 'part_id', 'offset', 'max_chars'}
            assert att['offset']['default'] == 0
            assert att['max_chars']['default'] == 12000
    run(go())


def test_gmail_lifespan_closes_on_exception():
    from john_agent.server import _gmail_lifespan

    closed = []

    class G:
        def close(self):
            closed.append(1)

    async def go():
        lifespan = _gmail_lifespan(G())
        with pytest.raises(RuntimeError):
            async with lifespan(None):
                raise RuntimeError('boom')
        assert closed == [1]

        closed.clear()
        async with _gmail_lifespan(G())(None):
            pass
        assert closed == [1]

    run(go())


GMAIL_ACCOUNT = 'me@example.com'
PDF_BYTES = b'%PDF-1.4 fake'


def _b64(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


class FakeGmailClient:
    def __init__(self):
        self.closed = False

    def get_profile(self):
        return {'emailAddress': GMAIL_ACCOUNT}

    def list_messages(self, query, limit, page_token, include_spam_trash):
        return {'messages': [{'id': 'm1'}], 'resultSizeEstimate': 1}

    def get_message(self, message_id, format='full'):
        headers = [
            {'name': 'From', 'value': 'a@b.c'},
            {'name': 'To', 'value': GMAIL_ACCOUNT},
            {'name': 'Subject', 'value': 'Hi'},
            {'name': 'Date', 'value': 'Mon, 1 Jan 2024 00:00:00 +0000'},
            {'name': 'Message-ID', 'value': '<m1@x>'},
        ]
        if format == 'metadata':
            return {'id': 'm1', 'threadId': 't', 'internalDate': '1',
                    'payload': {'partId': '', 'mimeType': 'text/plain',
                                'headers': headers, 'body': {'size': 0}}}
        return {'id': 'm1', 'threadId': 't', 'internalDate': '1',
                'payload': {'partId': '', 'mimeType': 'multipart/mixed',
                            'headers': headers, 'body': {'size': 0},
                            'parts': [
                                {'partId': '1', 'mimeType': 'text/plain', 'filename': '',
                                 'headers': [], 'body': {'size': 4, 'data': _b64(b'body')}},
                                {'partId': '2', 'mimeType': 'application/pdf',
                                 'filename': 'd.pdf', 'headers': [],
                                 'body': {'size': len(PDF_BYTES), 'data': _b64(PDF_BYTES)}},
                            ]}}

    def get_attachment(self, message_id, attachment_id):
        raise AssertionError('unexpected attachment fetch')

    def close(self):
        self.closed = True


def test_gmail_mcp_round_trip(reader):
    gmail = GmailReader(
        client_factory=FakeGmailClient,
        pdf_extractor=lambda data: ['page text'],
    )

    async def go():
        async with create_connected_server_and_client_session(
            create_server(reader, gmail_reader=gmail)
        ) as session:
            found = await session.call_tool('search_emails', {'query': 'hi'})
            assert found.isError is not True
            data = found.structuredContent
            assert data['source'] == 'gmail'
            assert data['results'][0]['citation_id'] == f'gmail:{GMAIL_ACCOUNT}:m1'

            read = await session.call_tool('read_email', {'message_id': 'm1'})
            assert read.structuredContent['excerpt']['text'] == 'body'
            assert read.structuredContent['attachments'][0]['supported'] is True

            att = await session.call_tool(
                'read_email_attachment', {'message_id': 'm1', 'part_id': '2'}
            )
            assert att.structuredContent['excerpt']['text'] == 'page text'
            assert att.structuredContent['citation_id'].endswith(':part:2')
            assert len(att.structuredContent['attachment_sha256']) == 64

            bad = await session.call_tool('search_emails', {'query': '  '})
            assert bad.isError is True
            assert 'invalid_input' in str(bad.content)

            corpus = await session.call_tool('corpus_status', {})
            assert corpus.isError is not True

    run(go())


def test_gmail_unexpected_error_sanitized(reader, caplog):
    class BoomGmail:
        def search_emails(self, *a, **k):
            raise RuntimeError('SECRET_SENTINEL detail')

        def close(self):
            pass

    async def go():
        async with create_connected_server_and_client_session(
            create_server(reader, gmail_reader=BoomGmail())
        ) as session:
            res = await session.call_tool('search_emails', {'query': 'q'})
            assert res.isError is True
            assert 'SECRET_SENTINEL' not in str(res.content)
            ok = await session.call_tool('corpus_status', {})
            assert ok.isError is not True

    with caplog.at_level('ERROR'):
        run(go())
    assert 'SECRET_SENTINEL' not in caplog.text


def test_gmail_env_secret_never_in_error(reader, monkeypatch, caplog):
    import httpx

    from john_agent.gmail_client import TOKEN_URL, GmailClient

    creds = json.dumps({
        'type': 'authorized_user',
        'client_id': 'MARKER_ID',
        'client_secret': 'MARKER_SECRET',
        'refresh_token': 'MARKER_REFRESH',
        'scopes': ['https://www.googleapis.com/auth/gmail.readonly'],
    })
    monkeypatch.setenv('JOHN_GMAIL_CREDENTIALS', creds)
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(400, json={
            'error': 'invalid_grant',
            'error_description': 'MARKER_ID MARKER_SECRET MARKER_REFRESH MARKER_ACCESS',
        })

    http = httpx.Client(transport=httpx.MockTransport(handler),
                        follow_redirects=False, trust_env=False, timeout=10.0)
    client = GmailClient(credentials=None, http_client=http)

    async def go():
        async with create_connected_server_and_client_session(
            create_server(reader, gmail_reader=GmailReader(client_factory=lambda: client))
        ) as session:
            res = await session.call_tool('search_emails', {'query': 'q'})
            assert res.isError is True
            text = str(res.content)
            assert 'authorization_required' in text
            for marker in ('MARKER_ID', 'MARKER_SECRET', 'MARKER_REFRESH', 'MARKER_ACCESS'):
                assert marker not in text

    with caplog.at_level('ERROR'):
        run(go())
    assert len(posts) == 1
    assert str(posts[0].url) == TOKEN_URL
    for marker in ('MARKER_ID', 'MARKER_SECRET', 'MARKER_REFRESH', 'MARKER_ACCESS'):
        assert marker not in caplog.text


def test_stdio_gmail_flag(md_dir: Path):
    env = {k: v for k, v in os.environ.items()
           if 'API_KEY' not in k and 'GEMINI' not in k and 'GMAIL' not in k}
    script = Path(sys.executable).parent / 'john-mcp'
    args = (
        ['--markdown-root', str(md_dir), '--gmail']
        if script.exists()
        else ['-m', 'john_agent.server', '--markdown-root', str(md_dir), '--gmail']
    )
    server = StdioServerParameters(
        command=str(script if script.exists() else sys.executable),
        args=args,
        env=env,
    )

    async def go():
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 8
                res = await session.call_tool('corpus_status', {})
                assert res.isError is not True
                gmail = await session.call_tool('search_emails', {'query': 'hi'})
                assert gmail.isError is True
                assert 'not_configured' in str(gmail.content)

    run(go())


def test_stdio_config_roundtrip(md_dir: Path, tmp_path: Path):
    config = {
        'version': 1,
        'corpus': {'markdown_root': str(md_dir)},
        'gmail': {'enabled': True},
    }
    cfg = tmp_path / 'john-mcp.json'
    cfg.write_text(json.dumps(config))
    cfg.chmod(0o600)
    env = {k: v for k, v in os.environ.items()
           if 'API_KEY' not in k and 'GEMINI' not in k and 'GMAIL' not in k}
    script = Path(sys.executable).parent / 'john-mcp'
    args = (
        ['--config', str(cfg)]
        if script.exists()
        else ['-m', 'john_agent.server', '--config', str(cfg)]
    )
    server = StdioServerParameters(
        command=str(script if script.exists() else sys.executable),
        args=args,
        env=env,
    )

    async def go():
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 8
                res = await session.call_tool('corpus_status', {})
                assert res.isError is not True
                assert res.structuredContent['manifest_documents'] == 2
                gmail = await session.call_tool('search_emails', {'query': 'hi'})
                assert gmail.isError is True
                assert 'not_configured' in str(gmail.content)

    run(go())


def test_stdio_smoke(md_dir: Path, tmp_path: Path):
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and "GEMINI" not in k}
    script = Path(sys.executable).parent / "john-mcp"
    server = StdioServerParameters(
        command=str(script if script.exists() else sys.executable),
        args=(
            ["--markdown-root", str(md_dir)]
            if script.exists()
            else ["-m", "john_agent.server", "--markdown-root", str(md_dir)]
        ),
        env=env,
    )

    async def go():
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "john"
                tools = await session.list_tools()
                assert len(tools.tools) == 5
                res = await session.call_tool("corpus_status", {})
                assert res.isError is not True
                assert res.structuredContent["manifest_documents"] == 2
                prompt = await session.get_prompt("review_opportunities", {})
                assert "savings" in prompt.messages[0].content.text

    run(go())


def test_help_works_without_credentials():
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k}
    proc = subprocess.run(
        [sys.executable, "-m", "john_agent.server", "--help"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    assert "--markdown-root" in proc.stdout
