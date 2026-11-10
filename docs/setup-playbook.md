# Setup playbook for AI agents

You are an AI agent helping one person set up **their own** Quanti Relay, in **their own** cloud
account. Nobody else hosts it. Follow these steps in order. Each step says what you do, what only
the person can do, and how to check it worked. Speak plainly: assume they have never used a
terminal.

## Rules you never break

1. **Secrets never pass through you.** Never ask for an exchange key, a password, a passphrase or
   a token in the chat, and never type one into a command. Each secret goes from the person
   straight into the server with `relay secret set <NAME>`, typed at **their** terminal (the
   value is hidden and never printed). That is a terminal window they open themselves, never one
   you run commands in: `relay secret reveal` shows a value on the terminal it runs in, so in yours
   it would reach you. If someone pastes a secret into the chat anyway, tell them to delete that
   key on the exchange and make a new one.
2. **Never switch anything to live trading.** Paper is the default and stays so. Going live is the
   person's own step, in the console, behind its gates. You may explain the gates; you never
   pass them for them.
3. **No trading numbers from you.** Sizes, stops, leverage and limits are the person's own
   decisions. Ask for each; an empty one means that rule is off. Never suggest a value.
4. **Ask before spending money.** Creating a server costs money: say which provider, plan and
   region you will create, and wait for a clear yes.
5. **Exchanges' rules.** Choose a server region the person's exchange allows for them. Never pick
   one to get around a country restriction.
6. **Stop when unsure.** When a check fails twice, or an answer surprises you, stop and explain.

## How you can help

- **You can run commands** on the person's computer (a coding agent): you run the cloud's
  command-line tool and SSH, and they type secrets at their own terminal when you ask.
- **You are a chat app without a terminal:** you guide them click by click through their
  provider's website and its browser console. Once the relay runs, they can add it as a connector
  (`https://<address>/mcp`), and you continue through it. Read "Without a terminal" below first:
  it says what changes in each step.

## Step 1: the cloud account (the person)

Explain `docs/cloud.md` ("What the server needs", "Recommended providers"). Let them choose any
provider that meets the needs. They create the account and add a payment method themselves.
**Check:** they can sign in to the provider's website.

## Step 2: access (the person, with you)

- An SSH key: if they have none, `ssh-keygen -t ed25519 -f ~/.ssh/quanti-relay` (they choose a
  passphrase at the prompt).
- The provider's command-line tool, if you will use it: they sign in themselves (most tools open
  the browser: `gcloud auth login`, `az login`, `aws configure sso`; for `hcloud` the
  person creates an API token in the website and pastes it at the tool's own prompt in **their**
  terminal, not to you).

**Check:** the tool lists their (empty) servers.

The login name on the server, `<USER>` in the commands below: `root` on Hetzner,
Vultr and Linode; `ubuntu` on AWS; `azureuser` (or the one chosen) on Azure. On Google Cloud use
`gcloud compute ssh relay --zone <ZONE> --command "sudo relay ..."` in place of `ssh`.

## Step 3: the server (you, after a yes)

Fill in `deploy/user-data.sh` (`<VERSION>`, the newest release; `--domain <DOMAIN>` only if they have
a domain pointing at the server). Create the server with the recipe for their provider in
`docs/cloud.md`. Note the server's public IPv4 address.

**Check (5 to 10 minutes later):** `curl -fsS https://<address>/health` answers `"status":"ok"`.
If not: `ssh <USER>@<address> sudo tail -n 50 /var/log/quanti-relay-install.log`, and make sure the
provider's firewall lets 80 and 443 in.

## Step 4: the relay's own check (you)

`ssh <USER>@<address> sudo relay doctor --json`. Read every check whose status is not `ok`: each has a
`fix`. At this point "no deployment", "exchange keys not set", "no sources" and "no heartbeat" are
expected.

The console: `https://<address>/console`. Its key is shown to the person on their terminal only:
they run `ssh -t <USER>@<address> sudo relay secret reveal RELAY_API_KEY` themselves.

## Step 5: the exchange (the person, guided)

1. They open an account on their exchange and pass its identity checks.
2. **First a test account:** the exchange's demo or testnet (`relay setup catalog` lists which
   each exchange offers). Start there.
3. They create an API key on that test account with: **trade** permission for the market they use
   (futures or spot), **no withdrawal**, and **locked to the server's IP address**. Some exchanges
   also ask for a passphrase.
4. They store each part at their own terminal. You give the exact commands; `relay doctor` names
   the variables (for example `BYBIT_API_KEY` and `BYBIT_API_SECRET`, or `RELAY_OKX_API_KEY`,
   `RELAY_OKX_API_SECRET`, `RELAY_OKX_API_PASSPHRASE`):
   `ssh -t <USER>@<address> sudo relay secret set <NAME>` (once per variable), then `ssh <USER>@<address> sudo relay restart`.

**Check:** `relay doctor --online --json` reports `key_trades` ok, `key_no_withdrawal` ok and
`key_ip_bound` ok. A key that can withdraw: they delete it and make a new one. Not known for this
exchange: they confirm it on the exchange's key page.

## Step 6: the deployment (you, with the person's numbers)

Ask which exchange, which environment (the test one first) and which market. Then ask for each
limit (see `examples/config/deployments/main.yaml` for what each means). Going live later needs
at least `rails.max_notional_usd` or `rails.max_notional_pct`, `rails.max_gross_leverage`,
`risk.max_positions` and `risk.daily_loss_limit_pct`. Write it with their numbers:

`ssh <USER>@<address> sudo relay setup write deployment main --file - < deployment.json`

The answer is JSON: `ok`, `changed`, `dry_run` (always `true` from here). Then `relay restart`.
**Check:** `relay setup show` and `relay doctor`.

## Step 7: who may send signals (you, the person for the secrets)

- **TradingView:** a source with `auth: passphrase`, the addresses TradingView publishes on its
  "About webhooks" help page in `allow_ips`, and `secret_env: RELAY_SOURCE_<NAME>_SECRET`. Make the
  passphrase with `relay secret generate RELAY_SOURCE_<NAME>_SECRET` (not shown); the person sees it
  with `ssh -t <USER>@<address> sudo relay secret reveal RELAY_SOURCE_<NAME>_SECRET` and pastes it into
  the alert's message themselves. Webhook URL: `https://<address>/v1/signals`.
- **Their own script or bot:** `auth: hmac`, the same way for the secret.

`relay setup write source <name> --file -`, then `relay restart`.

## Step 8: notifications (the person creates the bot)

Telegram: they create a bot with @BotFather and find their chat id; they store both with
`relay secret set RELAY_SINK_<NAME>_BOT_TOKEN` and `relay secret set RELAY_SINK_<NAME>_CHAT_ID`.
You write the sink: `{"type": "telegram", "bot_token_env": "RELAY_SINK_<NAME>_BOT_TOKEN",
"chat_id_env": "RELAY_SINK_<NAME>_CHAT_ID"}`. Discord and webhooks: `examples/config/sinks/`.

## Step 9: test on paper (you)

- `relay test-signal --source <name> --action open --symbol <SYMBOL> --side long --notional <USDT> --stop-pct <PERCENT>`
  with the person's own numbers: always paper. The console shows the position; the notification arrives.
- The venue check on the test account (console, Setup) for any exchange but native Bybit.
- A TradingView test alert: the console's Signals tab shows it.

## Step 10: when it stops (the person chooses a monitor)

They pick a heartbeat monitor service and create a heartbeat check; its URL goes to
`relay secret set RELAY_HEARTBEAT_URL` (it can hold a token), then `relay restart`.
**Check:** the monitor shows the calls arriving.

## Step 11: going live (the person alone)

Explain, do not do: in the console's Setup tab, live needs the keys, the core limits, the venue
check on the test account, and the deployment's name typed. Then the person sets
`RELAY_DRY_RUN=false` with `relay secret set RELAY_DRY_RUN` (or edits `.env`) and restarts. A
live key is a new key on the live account, made like the test one (trade only, no withdrawal,
locked to the server).

## Without a terminal (a chat app)

You cannot run commands. The person works in two places: their provider's website, with the
**browser console** it offers (a terminal on the server that opens in their browser), and later
the relay's own console. The steps above stay; this is what changes in them.

- **These pages:** the person gives you this playbook and `docs/cloud.md`, as links to the
  repository or as uploaded files.
- **Step 2, access:** no SSH key and no command-line tool are needed. The browser console is
  opened from the server's page on the provider's website. Some open signed in already (for
  example the "Connect using SSH" button on AWS Lightsail, the
  "SSH" button on Google Cloud). Others show a login prompt and need the server's password (for
  example Hetzner's console): the person sets or resets it on the provider's website and keeps it
  to themselves. Tell them which kind their provider has before they choose; when you are not
  sure, they look for "console" in the provider's help pages.
- **Pasting:** exchange keys are long. Before the first secret, the person pastes any word into
  the browser console to see that pasting works there. If it does not, they type the key by hand.
- **Step 3, the server:** write out `deploy/user-data.sh` filled in (`<VERSION>` is the newest
  release, from https://github.com/Quantitizer/quanti-relay/tags) as one block of text, for the person to
  copy into the field called "user data", "cloud-init" or "startup script" on the provider's create
  page. Name every choice on that page: Ubuntu 24.04, the size and region from `docs/cloud.md`
  (rule 5), ports 22, 80 and 443 in the provider's firewall. Rule 4 holds: they click create only
  after a clear yes. They tell you the server's IPv4 address.
  **Check:** they open `https://<address>/health` in their browser and tell you what it shows. If
  nothing answers after 10 minutes: `sudo tail -n 50 /var/log/quanti-relay-install.log` in the
  browser console; they may copy that to you (it holds no secret).
- **SSH passwords:** the install turns SSH logins with a password off (keys only), since the
  internet can guess passwords. The browser console still takes the password.
- **Every command:** where this playbook says `ssh <USER>@<address> sudo relay ...` (or `ssh -t`),
  the person types `sudo relay ...` in the browser console. Give one command at a time and say what
  it should answer. They may copy an answer back to you, except what `relay secret reveal` shows:
  that stays on their screen.
- **Steps 6 to 8, the settings:** you cannot hand over a file. Either guide them through the
  relay's console (`https://<address>/console`, the Setup tab), field by field with their own
  numbers, or continue as a connector:
  1. In the browser console: `sudo relay secret setting RELAY_MCP_ENABLED true`, then
     `sudo relay restart`.
  2. They add `https://<address>/mcp` as a custom connector in the chat app. The relay's approval
     page asks for the API key: they see it with `sudo relay secret reveal RELAY_API_KEY` and type
     it into that page, never into the chat. "Change the settings" stays ticked for the setup.
  3. You write the deployment, the sources and the notifications with the connector's
     `setup_write`, then `restart_relay`. Everything stays on paper, and secrets still go through
     `sudo relay secret set` in the browser console.

## When something is wrong

| What you see | What to do |
|---|---|
| `https://<address>/health` does not answer | the provider's firewall (80, 443); `relay logs`; `docker compose --profile tls logs caddy` |
| 429 "too many wrong API keys" | wait for the lockout to end (`Retry-After`); check the key with `relay secret reveal` |
| doctor: `key_ip_bound` fail | the key is locked to another address: add the server's address on the exchange |
| doctor: `sources` fail | a variable the source names is not set: `relay secret set`, then `relay restart` |
| `/health` says `stuck` | `relay restart`, then tell the person: it is reported as a critical event too |
| A change in `.env` does nothing | `relay restart` (it recreates the container, which reads `.env` again) |
