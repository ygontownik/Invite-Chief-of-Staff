---
description: Generate a visual JSX investment brief from diligence outputs for any registered TCIP deal
argument-hint: "<deal_id>"
---

# /deal-brief — Visual investment brief from diligence outputs

Generates a self-contained React JSX file: three tabs, under 60KB, using the canonical
Cholla design system. Source material is the deal's _Diligence/ files. Output is written
to the deal's Drive folder.

All Drive I/O uses:
```
python3 ~/cos-pipeline/tools/deal_extract_helpers.py
```

---

## STEP 0 — Parse argument

`$ARGUMENTS` must contain a `<deal_id>` (e.g. `cholla`, `pngts`, `unitil`).

Load the registry and resolve all file IDs:

```bash
python3 - <<'EOF'
import json, sys
data = json.load(open('/Users/ygontownik/cos-pipeline/tools/deal-system-data.json'))
deal_id = "$DEAL_ID"
deal = next((d for d in data['deals'] if d['deal_id'] == deal_id), None)
if not deal:
    sys.exit(f"deal_id '{deal_id}' not found in registry")
print(json.dumps(deal, indent=2))
EOF
```

Also resolve the `_Diligence/` folder ID and all registered diligence file IDs from drive-docs.yaml:

```bash
python3 -c "
import yaml, os, json
cfg = yaml.safe_load(open(os.path.expanduser('~/dashboards/config/drive-docs.yaml')))
deal_id = '$DEAL_ID'
entry = cfg.get('deal_docs', {}).get(deal_id, {})
df = entry.get('diligence_files', {})
print('diligence_folder_id:', entry.get('diligence_folder_id', 'NOT_SET'))
print('readme_id:', df.get('readme', 'NOT_SET'))
print('thesis_id:', df.get('thesis_pressure_test', 'NOT_SET'))
print('gaps_id:', df.get('gaps', 'NOT_SET'))
print('session_handoff_id:', df.get('session_handoff', 'NOT_SET'))
print('brief_jsx_file_id:', df.get('brief_jsx', 'NOT_SET'))
"
```

Hold in memory: `deal_id`, `name`, `deal_type`, `drive_folder_id`, `status_file_id`,
`diligence_folder_id`, `readme_id`, `thesis_id`, `gaps_id`, `session_handoff_id`,
`brief_jsx_file_id` (may be NOT_SET — create new file if so).

---

## STEP 1 — Read source material

Read all four _Diligence/ files. These are Google Docs — use `export_media` not `get_media`.
If a file ID is NOT_SET, log the gap and use empty/placeholder content for that section.

```python
import pickle, os, json, io
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from googleapiclient.http import MediaIoBaseDownload

token_path = os.path.expanduser('~/credentials/gdrive_token.pickle')
with open(token_path, 'rb') as f:
    creds = pickle.load(f)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
service = build('drive', 'v3', credentials=creds)

def read_gdoc(file_id):
    """Export a Google Doc as plain text for parsing."""
    request = service.files().export_media(fileId=file_id, mimeType='text/plain')
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue().decode('utf-8')
```

Read in this order:
1. `status_file_id` — extract: deal name, stage, health signal, entry metrics, hard deadline
2. `_Diligence/thesis_pressure_test.md` — extract: all drivers (D1-Dn), verdict per driver,
   rationale, bridge estimate per driver where present
3. `_Diligence/README.md` — extract: Quick Reference table (Metric / Value / Source / Confidence)
4. `_Diligence/gaps.md` — extract: all open gaps (GAP-NNN, description, priority, blocks)

After reading, hold in memory:
- **Drivers**: list of {id, name, verdict, rationale, bridge_value}
- **Metrics**: list of {metric, value, source, confidence}
- **Gaps**: list of {id, description, priority, blocks, eta}
- **Deal summary**: name, stage, health, entry metrics, thesis one-liner

---

## STEP 2 — Derive brief content

Before writing any JSX, resolve the content for each tab from the source material.

### Tab 0 — One-Pager

**Recommendation**: Synthesize from driver verdicts:
- If 0 CRITICAL gaps and majority CONFIRMED → "PROCEED — [one-line rationale]"
- If CRITICAL gaps open → "HOLD — pending [gap IDs]"
- If majority WEAKENED → "REASSESS — [what changed]"
- Always state the recommended next action in one sentence

**Scenario table** (bull / base / stress / walk):
- Derive from bridge estimates in thesis_pressure_test.md
- If bridge estimates are incomplete, use INCONCLUSIVE rows explicitly
- Columns: Scenario | Key Assumption | Bridge Value | Probability | Signal
- Color: bull=green row, base=neutral, stress=amber, walk=red

**Kill triggers** (3 rows max):
- Identify from CRITICAL gaps and WEAKENED drivers
- Columns: Trigger | Probability | Time to Detect

### Tab 1 — Driver Bridge

**Waterfall bars**: one bar per named driver, left to right
- Bar height/width = bridge_value from thesis_pressure_test.md (use 0 if INCONCLUSIVE)
- Color by verdict: CONFIRMED=#3C6E47 (green), WEAKENED=#B8841A (amber),
  INCONCLUSIVE=#9A9A9A (gray), NEW=#0B2545 (navy)
- Hover tooltip: driver name + one-line rationale + gap ID if open

**Running total**: cumulative bridge across all drivers

### Tab 2 — Key Assumption

- Identify the highest-priority INCONCLUSIVE driver that blocks a deal verdict
- If no INCONCLUSIVE drivers: show the highest-priority open CRITICAL gap instead
- Render as a single amber Callout with:
  - What the assumption is (one sentence)
  - Why it matters (the bridge value at stake or the driver it blocks)
  - Gap register entry (GAP-NNN, description, resolution path)
  - What would close it (specific data source or regulatory event)

---

## STEP 3 — Generate JSX

Produce a complete self-contained React JSX file. No external imports. All styling inline
or via a `styles` object. Use the canonical Cholla design tokens exactly as specified below.

### Design tokens (copy exactly — do not invent new colors)

```javascript
const C = {
  ice:       "#F5F4EF",
  paper:     "#FBFAF6",
  panel:     "#FFFFFF",
  navy:      "#0B2545",
  navyDeep:  "#06182E",
  navyMid:   "#13325C",
  amber:     "#B8841A",
  amberDeep: "#8C6210",
  amberSoft: "#E8C77A",
  amberPale: "#FBF3DF",
  green:     "#3C6E47",
  greenPale: "#EBF2EC",
  red:       "#7A1F1F",
  redPale:   "#FAEAEA",
  gray:      "#9A9A9A",
  grayPale:  "#F2F1EE",
  rule:      "#D9D5C8",
  ink:       "#1A1A18",
  inkMid:    "#3A3A35",
  inkFaint:  "#8A8A82",
};

const F = {
  serif: "'Georgia', 'Times New Roman', 'Palatino Linotype', serif",
  sans:  "'Helvetica Neue', 'Helvetica', 'Arial', sans-serif",
  mono:  "'SF Mono', 'Menlo', 'Monaco', 'Courier New', monospace",
};
```

### Required components

Implement these exactly — other tabs and JSX files follow the same pattern:

```javascript
// Section header with optional kicker line
const Section = ({ kicker, title, children }) => (
  <div style={{ marginBottom: 32 }}>
    {kicker && (
      <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing: 2,
                    color: C.amber, textTransform: "uppercase", marginBottom: 4 }}>
        {kicker}
      </div>
    )}
    <div style={{ fontFamily: F.serif, fontSize: 20, fontWeight: 400,
                  color: C.navy, borderBottom: `1px solid ${C.rule}`,
                  paddingBottom: 8, marginBottom: 16 }}>
      {title}
    </div>
    {children}
  </div>
);

// Sub-section header
const SubHead = ({ children }) => (
  <div style={{ fontFamily: F.sans, fontSize: 11, fontWeight: 700,
                color: C.inkMid, letterSpacing: 1, textTransform: "uppercase",
                marginBottom: 8, marginTop: 16 }}>
    {children}
  </div>
);

// Body paragraph
const Para = ({ children }) => (
  <p style={{ fontFamily: F.sans, fontSize: 13, lineHeight: 1.65,
              color: C.inkMid, margin: "0 0 12px 0" }}>
    {children}
  </p>
);

// Callout box — tone: "navy" | "amber" | "red" | "green"
const CALLOUT_PALETTE = {
  navy:  { bg: C.navyDeep, border: C.navyMid, text: "#FFFFFF",   kicker: C.amberSoft },
  amber: { bg: C.amberPale, border: C.amber,   text: C.navyDeep, kicker: C.amberDeep },
  red:   { bg: C.redPale,  border: C.red,      text: C.navyDeep, kicker: C.red       },
  green: { bg: C.greenPale,border: C.green,    text: C.navyDeep, kicker: C.green     },
};
const Callout = ({ tone = "navy", kicker, children }) => {
  const p = CALLOUT_PALETTE[tone];
  return (
    <div style={{ background: p.bg, borderLeft: `4px solid ${p.border}`,
                  padding: "14px 18px", marginBottom: 16, borderRadius: 2 }}>
      {kicker && (
        <div style={{ fontFamily: F.mono, fontSize: 9, letterSpacing: 2,
                      color: p.kicker, textTransform: "uppercase", marginBottom: 6 }}>
          {kicker}
        </div>
      )}
      <div style={{ fontFamily: F.sans, fontSize: 13, lineHeight: 1.6, color: p.text }}>
        {children}
      </div>
    </div>
  );
};

// Status pill — tone: "neutral" | "navy" | "amber" | "green" | "red" | "yellow"
const PILL_PALETTE = {
  neutral: { bg: C.grayPale,  text: C.inkMid  },
  navy:    { bg: C.navyDeep,  text: "#FFFFFF"  },
  amber:   { bg: C.amberPale, text: C.amberDeep },
  green:   { bg: C.greenPale, text: C.green    },
  red:     { bg: C.redPale,   text: C.red      },
  yellow:  { bg: "#FFFBE6",   text: "#7A5E00"  },
};
const Pill = ({ tone = "neutral", children }) => {
  const p = PILL_PALETTE[tone];
  return (
    <span style={{ fontFamily: F.mono, fontSize: 10, fontWeight: 700,
                   letterSpacing: 1, textTransform: "uppercase",
                   background: p.bg, color: p.text,
                   padding: "2px 8px", borderRadius: 2, marginRight: 4 }}>
      {children}
    </span>
  );
};

// Table, header cell, data cell
const Table = ({ children }) => (
  <table style={{ width: "100%", borderCollapse: "collapse",
                  fontFamily: F.sans, fontSize: 12, marginBottom: 16 }}>
    {children}
  </table>
);
const Th = ({ children, align = "left" }) => (
  <th style={{ textAlign: align, fontWeight: 700, fontSize: 10, letterSpacing: 1,
               textTransform: "uppercase", color: C.inkFaint,
               borderBottom: `2px solid ${C.rule}`, padding: "6px 8px" }}>
    {children}
  </th>
);
const Td = ({ children, align = "left", muted = false, mono = false }) => (
  <td style={{ textAlign: align, color: muted ? C.inkFaint : C.inkMid,
               fontFamily: mono ? F.mono : F.sans,
               borderBottom: `1px solid ${C.rule}`, padding: "7px 8px",
               fontSize: mono ? 11 : 12 }}>
    {children}
  </td>
);

// Source citation (small, faint)
const Src = ({ children }) => (
  <span style={{ fontFamily: F.mono, fontSize: 9, color: C.inkFaint,
                 letterSpacing: 0.5, marginLeft: 6 }}>
    [{children}]
  </span>
);

// Verify tag (intellectual honesty — unverified claim)
const Verify = ({ who }) => (
  <span style={{ fontFamily: F.mono, fontSize: 9, background: C.amberPale,
                 color: C.amberDeep, padding: "1px 5px", borderRadius: 2,
                 marginLeft: 4, letterSpacing: 0.5 }}>
    VERIFY: {who}
  </span>
);

// Footer
const Footer = ({ deal, date }) => (
  <div style={{ borderTop: `1px solid ${C.rule}`, marginTop: 32, paddingTop: 12,
                fontFamily: F.mono, fontSize: 9, color: C.inkFaint,
                letterSpacing: 1, display: "flex", justifyContent: "space-between" }}>
    <span>TCIP — {deal} — CONFIDENTIAL</span>
    <span>{date}</span>
  </div>
);
```

### Tab structure

```javascript
const TABS = [
  { id: "onepager",   label: "One-Pager"      },
  { id: "drivers",    label: "Driver Bridge"   },
  { id: "assumption", label: "Key Assumption"  },
];
```

### Tab 0 — One-Pager content

```
[Recommendation Callout — tone based on verdict: proceed=navy, hold=amber, reassess=red]
  kicker: "IC RECOMMENDATION"
  body: recommendation sentence + next action

[SubHead] The Ask
  [Para] deal name, stage, entry metrics (from status doc)

[SubHead] Scenario Analysis
  [Table]
    Th: Scenario | Key Assumption | Bridge | Probability | Signal
    Tr per scenario — background color by row: bull=C.greenPale, stress=C.amberPale, walk=C.redPale
    Td for Signal: Pill tone matching row

[SubHead] Kill Triggers
  [Table]
    Th: Trigger | Probability | Time to Detect
    Tr per trigger (max 3)

[Footer deal={deal_id} date={today}]
```

### Tab 1 — Driver Bridge content

Render a horizontal stacked-bar waterfall using inline SVG or div-based bar chart.
Each driver is one row. Include:
- Driver ID + name (left label)
- Verdict Pill (CONFIRMED=green, WEAKENED=amber, INCONCLUSIVE=neutral, NEW=navy)
- Bar: proportional to bridge_value; color matches verdict
- If bridge_value is 0 or INCONCLUSIVE: render a gray dashed-border bar of fixed minimal width
- Hover state: show rationale + gap ID (use React useState for tooltip)

At top: SubHead "Driver Verdicts — [Deal Name] — [Date]"
At bottom: running total line + verdict summary (N confirmed, N weakened, N inconclusive)

```
[Section kicker="THESIS PRESSURE TEST" title="Driver Bridge"]
  [per-driver row with bar + pill + rationale on hover]
  [running total]
  [verdict summary line]
[Footer]
```

### Tab 2 — Key Assumption content

```
[Section kicker="HIGHEST RISK" title="Key Assumption"]
  [Callout tone="amber" kicker="OPEN DRIVER — [DRIVER_ID]"]
    What: [assumption in one sentence]
    At stake: [bridge value or which driver it blocks]

  [SubHead] Gap Register Entry
  [Table]
    Th: Field | Detail
    Tr: Gap ID | [GAP-NNN]
    Tr: Description | [gap description]
    Tr: Priority | [Pill tone=red/amber] CRITICAL / HIGH
    Tr: Blocks | [driver or README row]
    Tr: Resolution path | [what to do]
    Tr: ETA | [date or "TBD — depends on X"]

  [SubHead] What Would Close This
  [Para] Specific filing name, data source, counterparty ask, or regulatory event
         that would allow driver verdict to move from INCONCLUSIVE to CONFIRMED/WEAKENED.

[Footer]
```

### Full component skeleton

The output file must be a complete working React component that:
1. Imports nothing — all logic is self-contained
2. Uses `React.useState` for active tab and hover state
3. Renders a tab bar at the top (navy underline on active tab)
4. Renders the active tab content below
5. Is valid JSX (no TypeScript, no JSX transform needed)

```javascript
// TCIP_brief_{deal_id}_{YYYY-MM-DD}.jsx
// Generated by /deal-brief — DO NOT EDIT MANUALLY

const { useState } = React;

// [tokens: C, F]
// [components: Section, SubHead, Para, Callout, Pill, Table, Th, Td, Src, Verify, Footer]

// [DATA — injected from _Diligence/ files — replace with real values]
const DEAL = {
  id: "{deal_id}",
  name: "{deal_name}",
  type: "{deal_type}",
  stage: "{stage}",
  health: "{health}",
  entryMetrics: "{entry_metrics}",
  thesis: "{thesis_one_liner}",
  date: "{YYYY-MM-DD}",
};

const DRIVERS = [
  // { id: "D1", name: "...", verdict: "CONFIRMED", rationale: "...", bridgeValue: 0, gapId: null }
];

const GAPS = [
  // { id: "GAP-001", description: "...", priority: "CRITICAL", blocks: "D2", eta: "...", resolutionPath: "..." }
];

const SCENARIOS = [
  // { label: "Bull", keyAssumption: "...", bridge: "$Xm", probability: "X%", signal: "..." }
];

const KILL_TRIGGERS = [
  // { trigger: "...", probability: "X%", timeToDetect: "..." }
];

// [Tab components: Tab0OnePager, Tab1DriverBridge, Tab2KeyAssumption]

const App = () => {
  const [activeTab, setActiveTab] = useState("onepager");
  const TABS = [
    { id: "onepager",   label: "One-Pager"     },
    { id: "drivers",    label: "Driver Bridge"  },
    { id: "assumption", label: "Key Assumption" },
  ];

  return (
    <div style={{ background: C.ice, minHeight: "100vh", padding: 32,
                  fontFamily: F.sans }}>
      {/* Deal header */}
      <div style={{ marginBottom: 24 }}>
        <div style={{ fontFamily: F.mono, fontSize: 10, color: C.amber,
                      letterSpacing: 2, textTransform: "uppercase", marginBottom: 4 }}>
          TCIP — {DEAL.type.toUpperCase()}
        </div>
        <div style={{ fontFamily: F.serif, fontSize: 28, color: C.navy,
                      fontWeight: 400, marginBottom: 4 }}>
          {DEAL.name}
        </div>
        <div style={{ fontFamily: F.sans, fontSize: 13, color: C.inkFaint }}>
          {DEAL.thesis}
        </div>
      </div>

      {/* Tab bar */}
      <div style={{ display: "flex", borderBottom: `2px solid ${C.rule}`,
                    marginBottom: 24, gap: 0 }}>
        {TABS.map(t => (
          <button key={t.id} onClick={() => setActiveTab(t.id)}
            style={{ fontFamily: F.sans, fontSize: 12, fontWeight: 600,
                     padding: "8px 20px", background: "none", border: "none",
                     cursor: "pointer", color: activeTab === t.id ? C.navy : C.inkFaint,
                     borderBottom: activeTab === t.id ? `3px solid ${C.navy}` : "3px solid transparent",
                     marginBottom: -2 }}>
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div style={{ maxWidth: 900, background: C.panel, padding: 32, borderRadius: 4 }}>
        {activeTab === "onepager"   && <Tab0OnePager />}
        {activeTab === "drivers"    && <Tab1DriverBridge />}
        {activeTab === "assumption" && <Tab2KeyAssumption />}
      </div>
    </div>
  );
};
```

---

## STEP 4 — Write JSX to Drive

The JSX brief stays as plain text (it's opened in a React sandbox, not in claude.ai
project sessions). Delete existing + recreate to keep the filename date-stamped.

```python
import pickle, os, yaml
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from googleapiclient.http import MediaInMemoryUpload

token_path = os.path.expanduser('~/credentials/gdrive_token.pickle')
with open(token_path, 'rb') as f:
    creds = pickle.load(f)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
service = build('drive', 'v3', credentials=creds)

jsx_content = """..."""  # the generated JSX string
filename = f'TCIP_brief_{DEAL_ID}_{DATE}.jsx'

# Delete existing if present
if BRIEF_JSX_FILE_ID != 'NOT_SET':
    try:
        service.files().delete(fileId=BRIEF_JSX_FILE_ID).execute()
    except Exception:
        pass

# Create new (plain text — opened in React sandbox, not project sessions)
media = MediaInMemoryUpload(jsx_content.encode('utf-8'), mimetype='text/plain')
meta = {'name': filename, 'parents': [DILIGENCE_FOLDER_ID]}
result = service.files().create(body=meta, media_body=media).execute()
new_id = result['id']
print(f"Created: {filename} ({new_id})")

# Register in drive-docs.yaml under diligence_files.brief_jsx
cfg_path = os.path.expanduser('~/dashboards/config/drive-docs.yaml')
cfg = yaml.safe_load(open(cfg_path))
cfg.setdefault('deal_docs', {}).setdefault(DEAL_ID, {}).setdefault('diligence_files', {})['brief_jsx'] = new_id
with open(cfg_path, 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
print(f"Registered in drive-docs.yaml")
```

---

## STEP 5 — Console summary

```
=== /deal-brief: <DEAL_NAME> (<deal_type>) ===

SOURCE MATERIAL:
  thesis_pressure_test.md — N drivers read (N CONFIRMED, N WEAKENED, N INCONCLUSIVE)
  README.md — N metrics read
  gaps.md — N gaps read (N CRITICAL, N HIGH)
  status doc — stage, entry metrics, thesis extracted

OUTPUT:
  File: TCIP_brief_{deal_id}_{YYYY-MM-DD}.jsx
  Drive ID: {file_id}
  Tabs: One-Pager | Driver Bridge | Key Assumption
  Size: ~NKB

BRIEF CONTENT SUMMARY:
  Recommendation: [PROCEED/HOLD/REASSESS] — one-line rationale
  Key Assumption (Tab 2): [driver or gap ID + one-line]
  Open CRITICAL gaps: N

RECOMMENDED NEXT STEPS:
  1. Open the JSX file in claude.ai Artifacts or a local React sandbox to review
  2. [any gaps or drivers that should be resolved before presenting externally]
```

---

## WHEN TO RUN

Run `/deal-brief` after any `/deal-diligence` session that updates driver verdicts or gaps.
The brief reads directly from the _Diligence/ files — run diligence first, then brief.

This is a Claude Code skill. It requires:
- Bash (Drive API Python calls)
- Read/Write (local file operations)
- WebFetch not required (all data from Drive, not web)

---

## RULES (non-negotiable)

- Never fabricate driver verdicts or bridge values. Use INCONCLUSIVE if data is missing.
- Use `Verify` tags for any claim that is not yet confirmed against a primary source.
- Never upgrade a recommendation to PROCEED if any CRITICAL gap is open.
- Cholla design tokens are canonical — do not invent new colors or override them.
- Output must be valid JSX with no external imports.
- File size target: under 60KB. If drivers or gaps are numerous, trim rationale text,
  not the data fields.
- "Per management" is not a source — use Src tags only for named filings or documents.
