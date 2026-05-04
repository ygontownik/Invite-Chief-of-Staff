"""mcp_server — read-only MCP prototype for the COS subscription sandbox.

Exposes three tools:
  - deal_pipeline.lookup(target_name)
  - transcripts.search(query, since_days=30)
  - lng_market.get_spot(region, date)  [stub — returns configured error]

Tool implementations are plain functions in `tools.py` so they can be
tested without MCP transport. `__main__.py` wires them into a FastMCP
server that speaks JSON-RPC over stdio.
"""
