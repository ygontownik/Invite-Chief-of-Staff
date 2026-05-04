"""Entry point: `python3 -m mcp_server` from inside the directory.

Wires the three tool functions into a FastMCP stdio server. JSON-RPC
in / JSON-RPC out — clients can be Claude Desktop, an SDK MCP client,
or any process that speaks the MCP protocol over stdio.
"""
import json

from mcp.server.fastmcp import FastMCP

from .tools import (
    deal_pipeline_lookup,
    lng_market_get_spot,
    transcripts_search,
)

mcp = FastMCP("cos-subscription-prototype")


@mcp.tool()
def deal_pipeline_lookup_tool(target_name: str) -> str:
    """Look up a deal/theme by name in the local deal-pipeline-data.json.

    Returns a JSON string. Best-effort substring match against id/theme/thesis.
    """
    return json.dumps(deal_pipeline_lookup(target_name))


@mcp.tool()
def transcripts_search_tool(query: str, since_days: int = 30) -> str:
    """Search the Transcripts Drive folder. Returns up to 10 hits as JSON.

    Read-only: uses drive.metadata.readonly scope from gdrive_token.pickle.
    """
    return json.dumps(transcripts_search(query, since_days))


@mcp.tool()
def lng_market_get_spot_tool(region: str, date: str) -> str:
    """Stub LNG spot quote — returns the configured 'not yet wired' error.

    Will be replaced when the LNG spreadsheet pipeline lands.
    """
    return json.dumps(lng_market_get_spot(region, date))


if __name__ == "__main__":
    mcp.run()
