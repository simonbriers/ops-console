# How to deploy a new client — the short version

No background knowledge needed. Total hands-on time: about 3 minutes.

## Deploy

1. Open the console: **http://localhost:8101**
   (If it's not running: open PowerShell, `cd C:\AI\ops-console`, then
   `docker compose -f docker-compose.local.yml up -d`)

2. Click the **Onboarding** tab.

3. Fill in three fields:
   - **Deploy name**: lowercase with hyphens, e.g. `smith-dental`
   - **Display name**: the business name, e.g. `Smith Dental`
   - **Subdomain**: the web address's first part, e.g. `smith`
     (the customer's chatbot will be at smith.my-ai-receptionist.com)

   Leave "Same VPS as" on whatever it shows. Click **Deploy**.

4. **The one thing it needs from you**: a DNS record. Go to the DNS panel
   for my-ai-receptionist.com and add:

   `A    <your subdomain>    46.225.234.151`

   You don't have to tell the console — it checks by itself every 20
   seconds and continues alone the moment the record works.

5. Wait about 5 minutes. Everything else is automatic: server setup,
   HTTPS certificate, credentials, testing. **All steps green = done.**

6. At the end, the card shows the chatbot's address and the admin
   password. That's what you give the customer.

## If something goes red

Read the one-line message on the red step, fix that one thing, and press
**"Run remaining steps"** again. Nothing is ever damaged by re-running —
it always continues from where it stopped. If the message doesn't make
sense, screenshot it and ask Claude.

(Two reds are NORMAL on a test deploy and can be ignored: "backup timer"
and "Caddyfile uncommitted".)

## To remove a client

Open its card → **Teardown this instance…** → type its deploy name →
**Confirm**. Everything is cleaned up: server, web address, monitoring.

## After deploying a real (non-test) client

- The business's details (opening hours, services, staff) are edited by
  the client themselves — or you — in their admin panel at
  `https://<subdomain>.my-ai-receptionist.com/admin`, no redeploy needed.
- Delete the DNS record only if you tore the client down.
