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
