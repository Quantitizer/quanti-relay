# Security

Quanti Relay places orders with real money behind them. Please report a weakness privately, so it
can be fixed before anyone can use it.

## Reporting a vulnerability

- **Use GitHub's private reporting.** Open this repository's **Security** tab and choose **Report
  a vulnerability**. Only the maintainers see the report.
- **Do not open a public issue,** pull request or discussion for it.
- **Say what you found:** what an attacker can do, the steps to reproduce it (against a paper or
  test setup, never someone else's relay), and the version or commit.
- **Expect an answer within a week.** We will tell you when a fix is out and, if you like, credit
  you in the release notes.

## What counts

For example:
- anything that lets a request that was not signed by a registered source, or sent without the
  API key, reach an order, a close or a setting;
- anything that leaks a secret: an exchange key, a source secret, a passphrase, the API key;
- anything that leaves a position without its stop on the exchange while the relay believes it
  has one;
- anything that makes the relay trade live while its settings say paper.

Bugs that need the owner's own keys or shell access to exploit are still welcome as normal issues.

## For people running a relay

- Keep keys and secrets in environment variables only, never in a file in a repository.
- Keep the relay on localhost (the default) and put a TLS reverse proxy in front of it to reach
  it from elsewhere. Outside localhost it refuses to start without `RELAY_API_KEY`.
- Give each source its own secret of at least 32 characters, and each relay its own secrets.
- Use exchange API keys that cannot withdraw, restricted to your server's address where the
  exchange allows it.
- Stay on paper (`dry_run: true`) and a test account until `relay verify-venue` has passed on the
  exchange you use.
