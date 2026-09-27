# Add-a-recipe relay

A tiny Cloudflare Worker that lets the site's **+ Add a recipe** form work without sending anyone to GitHub.

Flow: site form → relay (`/add`) → human check (Turnstile) + rate limit + "is it a recipe?" check → opens an `add-recipe` issue → the GitHub Action imports it and replies → the site polls the relay (`/status`) and shows the reply.

Pages that aren't recipes are turned away before anything is filed.

## One-time setup

1. **Cloudflare account:** sign up free at https://dash.cloudflare.com/sign-up.
2. **Turnstile (human check):** Dashboard → Turnstile → Add widget.
   - Name: `grill-recipes`, hostname: `rsissons.github.io`, mode: Managed.
   - Keep the **Site Key** (public) and the **Secret Key** (private).
3. **GitHub key:** https://github.com/settings/personal-access-tokens/new
   - Name `grill-recipes relay`, expiration 1 year.
   - Repository access: *Only select repositories* → `grill-recipes`.
   - Permissions → Repository → **Issues: Read and write**. Nothing else.
4. **Deploy** (PowerShell, in this folder):
   ```
   npm install
   npx wrangler login
   npx wrangler secret put GH_TOKEN          # paste the GitHub key
   npx wrangler secret put TURNSTILE_SECRET  # paste the Turnstile Secret Key
   npx wrangler deploy                       # prints the relay address
   ```
5. In `index.html`, set `RELAY` to the address that deploy printed and `TURNSTILE_KEY` to the Turnstile **Site Key**. Both are public.

Secrets live only in Cloudflare. Never commit them. `.dev.vars` (local testing) is git-ignored.

## Local testing

Put `GH_TOKEN=...` and `TURNSTILE_SECRET=1x0000000000000000000000000000000AA` (Cloudflare's always-pass test secret) in `.dev.vars`. Then run `npx wrangler dev --port 8787`. On the page, use the test site key `1x00000000000000000000AA`.
