# Running the relay in your own cloud

The relay runs on a server **you** rent, in **your** cloud account. Nobody else hosts it, and the
project never sees your server, your keys or your trades. This page says what such a server needs,
which providers are a good start, and how the one step that differs between them (creating the
server) is done on each. Everything after that is the same everywhere: `deploy/install.sh`.

An AI agent can do all of it with you: `docs/setup-playbook.md` is written for it.

## What the server needs

Any provider that offers these works, not only the ones listed below:

| Need | Why |
|---|---|
| A Linux server that is always on: Ubuntu 24.04 or 22.04 LTS, or Debian 13 (on Debian 12, install Docker from docs.docker.com first) | the relay watches positions and stops around the clock |
| A public IPv4 address that stays the same | exchange keys are locked to it; senders such as TradingView call it |
| Ports 80 and 443 open to the internet | HTTPS: the certificate is fetched on 80, the relay answers on 443 (over IPv4; do not give your domain an AAAA record) |
| A persistent disk | the database (positions, signals, events) and the certificates |
| "User data" (cloud-init) when the server is created, or SSH access | to run the install script |
| A browser console on the provider's website, if you have no terminal | to type the relay's commands and your secrets on the server |
| About 1 vCPU and 2 GB of memory | building the container needs the memory; running it needs little |
| A region where your exchange serves you | exchanges refuse some countries' addresses. Pick a region your exchange's terms allow for you; never use a server to get around a restriction |

Choose a region near the exchange's servers if you can: orders arrive sooner.

## Recommended providers

A suggestion, not a limit. Each of these offers everything above; prices and plans change, so look
them up on the provider's own page. "Rehearsed" means the whole path was run end to end on it.

| Provider | Address that stays | User data | Notes | Rehearsed |
|---|---|---|---|---|
| Hetzner Cloud | the server's primary IPv4 | yes | simple, EU and US regions | not yet |
| Vultr | the instance's IPv4 (or a Reserved IP) | yes | many regions | not yet |
| Akamai (Linode) | the Linode's IPv4 | yes (Metadata) | many regions | not yet |
| AWS Lightsail | a Static IP you attach | yes | the simple side of AWS | not yet |
| Google Cloud | a static external address you reserve | yes (startup script) | runs it at every start (the script allows that) | not yet |
| Microsoft Azure | a Standard public IP (static) | yes (custom data) | | not yet |
| OVHcloud, Scaleway, IONOS | the instance's IPv4 | yes | EU providers | not yet |

## The one step that differs: creating the server

First fill in `deploy/user-data.sh`: `<VERSION>` is the release you want (the newest tag on
https://github.com/Quantitizer/quanti-relay/tags). Add `--domain <DOMAIN>` to its last line if you
use your own domain (its DNS A record must point to the server's address). Then, on your provider:

**In the provider's website (any provider):** create a server with Ubuntu 24.04, your SSH key, and
the filled-in `user-data.sh` pasted into the field called "user data", "cloud-init" or "startup
script". Open ports 22, 80 and 443 if the provider has its own firewall. Wait a few minutes. No SSH
key? Then you work in the provider's browser console (`docs/setup-playbook.md`, "Without a
terminal"); the install turns SSH logins with a password off, and that console keeps working.

**With the provider's command-line tool** (`<...>` are your own choices; the tool must be signed in
to your account first):

- Hetzner Cloud (`hcloud`):
  `hcloud server create --name relay --type <TYPE> --image ubuntu-24.04 --location <LOCATION> --ssh-key <KEY> --user-data-from-file deploy/user-data.sh`
- AWS Lightsail (`aws`): create, give it a static address, open the ports:
  `aws lightsail create-instances --instance-names relay --availability-zone <ZONE> --blueprint-id ubuntu_24_04 --bundle-id <BUNDLE> --user-data file://deploy/user-data.sh`
  `aws lightsail allocate-static-ip --static-ip-name relay-ip`
  `aws lightsail attach-static-ip --static-ip-name relay-ip --instance-name relay`
  `aws lightsail open-instance-public-ports --instance-name relay --port-info fromPort=443,toPort=443,protocol=TCP`
  `aws lightsail open-instance-public-ports --instance-name relay --port-info fromPort=80,toPort=80,protocol=TCP`
- Google Cloud (`gcloud`): reserve the address, create the server with it, open the ports:
  `gcloud compute addresses create relay-ip --region <REGION>`
  `gcloud compute instances create relay --zone <ZONE> --machine-type <TYPE> --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud --address relay-ip --tags relay --metadata-from-file startup-script=deploy/user-data.sh`
  `gcloud compute firewall-rules create relay-https --allow tcp:80,tcp:443 --target-tags relay`
- Microsoft Azure (`az`):
  `az vm create --resource-group <GROUP> --name relay --image Ubuntu2404 --size <SIZE> --public-ip-sku Standard --custom-data deploy/user-data.sh --generate-ssh-keys`
  `az vm open-port --resource-group <GROUP> --name relay --port 80,443`

**Without user data:** connect with SSH and run, as root, the lines of `deploy/user-data.sh`.

## After the server is up

On the server, as root (`sudo` before each command if you log in as another user), in a terminal
window you opened yourself or the provider's browser console, not one an AI agent runs (secrets are
typed and shown there):

1. `relay doctor` says what is set and what is missing.
2. `relay secret reveal RELAY_API_KEY` shows the console's key on your terminal: sign in at
   `https://<address>/console`.
3. `relay secret set <NAME>` for each exchange key the doctor names: you paste it, it is not
   shown, and it never passes through a chat. Then `relay restart`.
4. Set up the exchange, the limits, the sources and the notifications in the console (or with
   `relay setup`), test on paper, and run the venue check on the exchange's test account.

Going live stays your own step, in the console, behind its gates.

## The address

- **By default** the relay answers at `https://<the server's IP address>`, with a certificate from
  Let's Encrypt for the address itself. It is free and renews by itself every few days; port 80
  must stay open for that, and if Let's Encrypt cannot be reached for a few days in a row the
  certificate runs out (a domain's certificate lasts 90 days and leaves far more room).
- **If a sender will not take an IP address:** `deploy/install.sh --sslip` uses the free name
  `<ip-with-dashes>.sslip.io` instead (a public service that answers with the address in the name).
  Its certificates share Let's Encrypt's weekly limit with every other sslip.io user, so one can be
  refused on a busy week; it is then tried again later.
- **Your own domain** (optional, a few euros a year): point its A record to the server's address and
  run `deploy/install.sh --domain <DOMAIN>`. This is the steadiest choice for the long run.

## Keeping it running

- **Updates:** the server installs security updates by itself. The relay is updated only when you
  say so: `relay update` shows what would change, `relay update <VERSION> --yes` does it (a copy of
  the database first).
- **When it stops:** set `RELAY_HEARTBEAT_URL` to a heartbeat monitor of your choice (a service
  that alerts you when the calls stop). It covers what the relay cannot say itself: that it is down,
  or its server is gone. `/health` also says "stuck" when its background work stops.
- **Backups:** a copy of the database is made before every schema upgrade and with
  `relay db backup`. Copy `/opt/quanti-relay/.env` and `config/` somewhere safe yourself, or use
  your provider's snapshot feature.
- **Moving or removing it:** close your positions first. Deleting the server deletes the relay;
  delete or lock the exchange keys too.
