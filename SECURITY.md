# Security policy

## Reporting a problem

Please report security problems privately, not in a public issue: open the repository's **Security** tab
and choose **Report a vulnerability**. Include what you found, how to reproduce it, and the Shoebox version
(`shoebox.py --version`). You should get a reply within a week.

Never include real passwords, tokens or backup files in a report.

## Supported versions

Only the latest release gets fixes. Upgrade with `git pull` (see the README).

## What Shoebox does and does not protect

- **Backups are not encrypted.** Anyone with access to the Dropbox app folder, or the local backup folder,
  can read them. That is by design: see the README's Security section.
- Secrets live only in `.env` (mode 600). They are never written to the XML config, the log, the report or
  a command line. Database passwords reach the client through `MYSQL_PWD` in the environment.
- The Dropbox login uses OAuth2 PKCE with an App-folder app, so a leaked token can reach only that app's
  folder.

A way around any of these is a security problem worth reporting.
