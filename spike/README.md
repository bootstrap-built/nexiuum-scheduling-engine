# 1.5B0 — SSE-from-iframe CSP Spike

**Phase 1.5 Open Item #2 (v3 plan).** Validates whether a Monday Apps Framework board-view iframe can open external EventSource connections to a non-Monday origin. Outcome drives the embedded-view architecture for 1.5B3.

## Artifact

`index.html` — single-page test running three checks side-by-side:

1. **External SSE** (`sse.dev/test`) — primary signal. EventSource to a third-party HTTPS streaming endpoint.
2. **Same-origin fetch** — sanity check that the iframe loads and runs JS.
3. **Monday SDK context** — confirms the iframe is actually inside a Monday board view (not embedded raw on a public URL).

## Hosting

Published via GitHub Pages from the `spike/` directory of this repo:

- URL: `https://bootstrap-built.github.io/nexiuum-scheduling-engine/` (after Pages is enabled)
- Workflow: `.github/workflows/pages.yml` deploys on every push to `main` that touches `spike/**`

## Monday app setup — manual steps for Josh

The Monday Apps Framework requires going through the developer portal in browser. Can't fully automate this.

1. Open [auth.monday.com/developers/apps](https://auth.monday.com/developers/apps) signed in as a Gray Space admin.
2. Click **Create App** → name it `Scheduling Engine Spike` (or anything). Pick the Gray Space workspace as the home.
3. In the new app, **Features** → **Add feature** → **Board View**.
4. In the board view config:
   - **URL:** `https://bootstrap-built.github.io/nexiuum-scheduling-engine/`
   - **Permissions scope:** Read board info / Read items (minimum needed for the SDK context call)
5. **Versions** tab → promote the draft version to Live (or install as draft to a workspace board).
6. **Install app** to the Gray Space workspace.
7. Go to the Schedule board (`18413802995`) in Gray Space. Click **+** to add a view → look for `Scheduling Engine Spike` in the list → add it.
8. Open the new view tab. Three boxes should render and report status.

## Interpreting results

| Outcome | Meaning |
|---|---|
| **All three OPEN/OK + Test 1 logs 5 events** | Full green. CSP allows external SSE. v3 architecture is viable. Proceed with 1.5B2 engine build. |
| **Test 1 CLOSED, Tests 2/3 OK** | Monday's CSP blocks external `connect-src`. Pivot needed: either proxy SSE through monday-code (hosted by Monday) OR fall back to short-polling for change notifications. |
| **Tests 2/3 ERROR** | Iframe isn't running scripts correctly. Possibly the app wasn't installed right; re-check the app URL and version state. |
| **Test 3 NO CONTEXT** | The iframe loaded but didn't receive Monday context — usually means the app's permission scope is too narrow. Add `boards:read` and reinstall. |

Report back what each box shows. I'll interpret + decide next steps from there.

## Cleanup when done

After the spike's question is answered, the Monday app can be uninstalled from the workspace and deleted from the developer portal. The HTML stays in the repo as a reference / regression test.
