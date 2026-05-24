"""Entry point: python3 -m _subscription.drive_mcp_extended"""
from typing import List, Optional

from mcp.server.fastmcp import FastMCP

from .tools import list_folder_contents, read_any_file, update_file

mcp = FastMCP("drive-extended")


@mcp.tool()
def read_any_file_tool(fileId: str, mimeType: Optional[str] = None) -> str:
    """Read any Drive file — Docs, Sheets, PDF, XLSX, images, plain text.

    Returns JSON: {fileId, title, mimeType, content, pages (PDF only),
    sheets (XLSX only), warnings}.
    Images are returned as base64. Unknown binary types return hex of first 500 bytes.
    Never silently fails — always returns JSON with a warnings list.
    """
    return read_any_file(fileId, mimeType)


@mcp.tool()
def list_folder_contents_tool(
    folderId: str,
    recursive: bool = False,
    fileTypesFilter: Optional[List[str]] = None,
) -> str:
    """List files in a Drive folder.

    Returns JSON: {folderId, files: [{fileId, title, mimeType, size,
    modifiedTime, parents, recent}]}.
    recursive=true traverses subfolders (max depth 3).
    fileTypesFilter: ["pdf", "xlsx", "gdoc"] — filters by extension or Google type.
    Never returns folder objects, only files.
    Files modified in last 7 days have recent=true.
    """
    return list_folder_contents(folderId, recursive, fileTypesFilter)


@mcp.tool()
def update_file_tool(
    fileId: str,
    textContent: Optional[str] = None,
    base64Content: Optional[str] = None,
    contentMimeType: str = "text/plain",
    title: Optional[str] = None,
) -> str:
    """Update an existing Drive file in place. Never calls files.create().

    For Google Docs with contentMimeType=text/plain: replaces document content.
    For plain files (txt, json, md): replaces file bytes directly.
    Returns JSON: {success, fileId, title, modifiedTime, writeConfirmed}.
    Returns {success: false, error: ...} if fileId not found or write fails.
    """
    return update_file(fileId, textContent, base64Content, contentMimeType, title)


if __name__ == "__main__":
    mcp.run()
