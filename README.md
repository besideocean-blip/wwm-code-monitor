# Where Winds Meet Code Monitor

This project monitors Where Winds Meet redemption codes with GitHub Actions, a GitHub Issue, a Discord Webhook, and a Cloudflare Worker.

The automatic scanner uses [codes.yar.gg](https://codes.yar.gg/) and the
[latest replies in the Bahamut code thread](https://forum.gamer.com.tw/C.php?bsn=75703&snA=388&last=1).
PC Gamer and Arlen remain disabled.

## Features

- Reads YAR's public active/expired lists and Bahamut's new reply floors every 30 minutes.
- Compares codes with the state stored in a GitHub Issue.
- Keeps the existing green Discord cards, titles, one-code-per-line layout and timestamps.
- Adds a source link and a reply-floor footer to Bahamut cards.
- Deduplicates codes across YAR, Bahamut and manual reports, including previously expired codes.
- Removes codes marked as confirmed expired by the site from the Issue state. A player's personal "used" marker does not mean a code is expired.
- Establishes a baseline on the first scan after switching sources, so the site's existing codes are not all announced at once.
- Accepts player reports through the Discord `/report` command. Reports use the same stored-code check before any announcement.
- Establishes an independent, quiet Bahamut baseline on its first successful scan.
- Follows older pages until it reaches the saved reply floor, so a page boundary does not skip new replies.
- Offers a `dry_run` preview that reads sources without posting to Discord or writing the Issue.

Both sources are community-maintained. A code listed as active has not necessarily
been verified with your game account. Bahamut candidates retain an internal `unverified` status;
they cannot expire codes from other sources. A failed source keeps its existing
state and does not stop the other source. If both sources fail, the run fails
without changing the state. The Actions summary shows each source's result.

## How It Works

Automatic scan:

```text
codes.yar.gg lists + Bahamut new reply floors
GitHub Actions
GitHub Issue state
Discord Webhook announcement
```

Player report:

```text
Discord /report
Cloudflare Worker
GitHub Actions manual_codes input
GitHub Issue comparison
Discord Webhook announcement
```

## GitHub Issue State

The workflow creates or updates an Issue titled:

```text
[WWM Monitor] State - do not edit
```

The Issue records current codes, a compact code history for deduplication, and the
last successfully scanned Bahamut floor. Existing state migrates automatically;
do not delete the Issue when upgrading or edit its JSON manually.

## Setup

In the repository's `Settings` -> `Environments`, create an environment named:

```text
discord-production
```

Add an environment secret named `DISCORD_WEBHOOK_URL` and set its value to your Discord channel's Webhook URL. Keep GitHub Actions and the `Scan WWM redemption codes` workflow enabled for scheduled scans and player reports.

## Add Codes Manually

In GitHub, open:

```text
Actions -> Scan WWM redemption codes -> Run workflow
```

Paste one or more codes into `manual_codes`, with one code per line if preferred. New codes are saved to the Issue and announced in Discord; known codes are not announced again.

## Discord `/report`

The Cloudflare Worker in `discord-report-worker/` handles the Discord `/report` command. Players enter suspected new codes in its `codes` field. The Worker starts the GitHub Actions workflow with the `manual_codes` input, and the workflow checks the Issue before posting to Discord.

See `discord-report-worker/README.md` for Worker setup.

## Schedule

The workflow is scheduled every 30 minutes, at minutes 17 and 47:

```yaml
cron: "17,47 * * * *"
```

GitHub Actions interprets this schedule in UTC, which is eight hours behind Taiwan time.
Scheduled runs can be delayed; this is not an exact delivery-time guarantee.

## Preview and Source Status

Open `Actions -> Scan WWM redemption codes -> Run workflow`, leave `manual_codes`
empty and check `dry_run`. Inspect the run summary for separate YAR and Bahamut
results. Preview does not require a webhook and does not establish a saved baseline.
Run again with `dry_run` unchecked to save the first successful baseline.

Optional repository Actions variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BAHAMUT_SCAN_ENABLED` | `true` | Set to `false` to keep YAR only. |
| `BAHAMUT_MAX_PAGES` | `8` | Catch-up page limit, from 1 to 12. A limit/fetch/parse failure leaves the cursor unchanged. |

## Bahamut Scope and Limitations

- Scans reply floors with text content, not the first-floor summary, small B1/B2
  comments, images, or later edits to already-processed floors.
- Removes quoted passages, links and crossed-out codes. It skips expired-code lines
  and uses conservative token heuristics. Letter-only codes need an explicit code
  label. These rules can miss codes; YAR remains the structured backup.
- Uses the existing project's `data-floor` and `c-article__content` selectors.
  Synthetic fixture tests do not prove that the live site's layout is unchanged.
- The live check on 2026-09-28 (Taiwan time) read YAR successfully but received
  HTTP 403 from Bahamut. This version does not bypass access checks. Its Bahamut
  connection and current HTML layout still need verification in your Actions runner.
- State is saved after notifications succeed so a Discord failure can be retried.
  If Discord accepts a message but its response or the subsequent state write fails,
  a retry may duplicate it. Exactly-once delivery is not guaranteed.

## Tests

```sh
node --test
```

No new npm dependencies or secrets are required. Tests use mocked network requests
and do not send real Discord messages.

## Security

Never commit a Discord Webhook URL, Discord Bot Token, GitHub Token, `.env` file, API key, or other secret. Store secrets in GitHub Secrets or Cloudflare Worker Secrets. Rotate any token or Webhook URL that has been exposed.

## License

MIT License
