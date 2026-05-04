# subscription mcp_server — read-only prototype

Run:

```
cd ~/cos-pipeline/_subscription/mcp_server && python3 -m mcp_server
python3 test_tools.py   # 6 tests, all must pass
```

## Tool schema

| Tool | Args | Returns | Side effects |
|---|---|---|---|
| `deal_pipeline_lookup_tool` | `target_name: str` | JSON string with `matches`, `count` (or `error`) | Reads `~/cos-pipeline/deal-pipeline-data.json` (override via `$DEAL_PIPELINE_DATA_PATH`). |
| `transcripts_search_tool` | `query: str`, `since_days: int = 30` | JSON string with `hits` (≤ 10) (or `error`) | Drive metadata-only call against folder `1B7UgpFCElgyZMLbq1yrf-N7PsB-UA4SE`. Read-only scopes. |
| `lng_market_get_spot_tool` | `region: str`, `date: str` | JSON string `{"error":"lng spreadsheet not yet wired", ...}` | None — stub. |

Sandbox-only. Not wired into any daemon, dashboard, or LaunchAgent.
