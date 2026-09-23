# Food photos from a phone

Goal: attach a photo in ChatGPT on iPhone, estimate the eaten portion, and save it to the existing Raspberry Pi database. No Render deployment or separate OpenAI API key is needed for the GPT Action route.

## Platform prerequisite — check before changing the Pi

The Calorie Logger plugin displayed “Only available on desktop app” on this account. An OAuth connection does not override this restriction. There is no verified mobile-enable manifest switch in this repository.

An **existing, still-editable custom GPT with Actions** is an alternative mobile entry point. Open the original calorie GPT on the web and check for Edit GPT → Actions. Do not assume that the migrated plugin page is a GPT editor. As of September 23, 2026, OpenAI says personal accounts cannot create new GPTs; existing GPTs can still be edited and used on mobile. GPT retirement is planned, so this is a transitional route, not a permanent guarantee.

If the original GPT no longer has an Actions editor, stop here. Repository changes cannot create that account capability or remove the plugin's desktop restriction. A separately hosted photo-upload app would require a separately configured vision service and is a different workflow; do not silently substitute it.

References:
- https://help.openai.com/en/articles/8554397-creating-and-editing-gpts
- https://developers.openai.com/api/docs/actions/introduction

## Connect the existing GPT to the Pi

1. In the existing GPT's web editor, use Actions (not the desktop plugin connection). Apps and Actions cannot be enabled together in one GPT.
2. Import `https://stewytailscale.tail93bb57.ts.net/action-openapi.json`.
3. Confirm its server URL is `https://stewytailscale.tail93bb57.ts.net` — no Render host and no port 8443.
4. Select API key authentication with custom header `X-API-Key`. Enter the Pi bridge's `APP_API_KEY` privately in the editor. This is not the dashboard password or an OpenAI API key. Never put it in GPT instructions, GitHub, or chat.
5. Paste `integrations/mobile-gpt/instructions.txt` into the GPT instructions and save the existing GPT.
6. Test `getDailySummary` first. This is read-only. Open that same GPT on the phone, attach a food photo, and say “Log this”. Approve a tool call if ChatGPT prompts.
7. Verify the response has a saved meal ID and the meal appears in the Pi dashboard. Compare the updated summary. Do not call the workflow complete until this phone test succeeds.

The photo is interpreted inside ChatGPT; the Action sends the resulting structured meal to `/api/meals`. Photos are not uploaded to or analyzed by the Pi. FatSecret mirroring remains optional and runs after local storage.

## Read-only Pi check

Run `bash scripts/pi-check-mobile-action.sh` from this repository. It checks both local and public schema, public authentication enforcement, and an authenticated summary. It does not create test meals or print secrets. It uses the running bridge container's configured public URL and fails if that URL still points away from the expected Pi hostname.

If the schema URL is already correct, no bridge redeployment is needed to configure the GPT. The source fix ensures future deployments honor PUBLIC_BASE_URL when generating the schema behind a proxy.

## Preserve the desktop login fixes

This branch also retains `Referrer-Policy: strict-origin` and a CSP allowing form redirects to `https://chatgpt.com`. It preserves Origin and CSRF validation. Rebuild the adapter with `bash scripts/pi-update-work-mcp.sh` only when deploying those source changes. Existing working adapter state and routes need not be changed for the Action route.
