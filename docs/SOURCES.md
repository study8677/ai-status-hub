# Official Sources and Signal Boundaries

This project monitors official public status sources only. It does not call paid model APIs or rely on third-party aggregators.

## OpenAI

- Source: `https://status.openai.com/api/v2/summary.json`
- Type: Statuspage JSON
- Signal: page indicator, component status, active incidents
- Boundary: provider incidents may target a subset of products or workspaces. The monitor preserves active incident details so downstream readers can decide scope.

## Claude

- Source: `https://status.claude.com/api/v2/summary.json`
- Type: Statuspage JSON
- Signal: page indicator, component status, active incidents
- Boundary: `indicator=none` means the base page status is operational, but active major incidents still affect the normalized status.

## Gemini

- Source: `https://status.cloud.google.com/incidents.json`
- Type: Google Cloud incident JSON
- Signal: active incidents filtered by product names containing Gemini / Vertex Gemini
- Boundary: broad Google Cloud incidents are ignored unless the affected product list identifies Gemini-related services.

## Grok

- Primary source: `https://status.x.ai/feed.xml`
- Fallback source: `https://status.x.ai/`
- Type: RSS + page text fallback
- Signal: unresolved RSS incidents first; page fallback only when RSS is unavailable
- Boundary: the page may be Cloudflare-blocked from automation environments. A successful RSS feed with only resolved incidents is treated as OK.

## AWS

- Source: `https://health.aws.amazon.com/public/currentevents`
- Type: AWS public health JSON, UTF-16 encoded
- Signal: active public current events and event log timestamps
- Boundary: this is full AWS public health, not AI-only AWS service filtering. It can report regional infrastructure incidents that are relevant to cloud users but not specific to Bedrock or AI APIs.

## Unknown vs Provider Outage

Fetch or parse failures are reported as `unknown`. They represent monitor-source health and do not directly create provider outage alerts.
