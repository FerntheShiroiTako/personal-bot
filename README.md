# Personal lookup bot

A private Discord bot for Roblox and Discord lookups across the five Rayward sources: Rotector, TASE, RAB, Okappiki and ServerSweep. It installs to your own Discord account, so its commands work in any server, DM or group DM. Only the account set in `OWNER_ID` can use it.

## Commands

One command does everything:

```
/check [user] [platform] [visibility] [everyone]
```

- `user` is a Roblox username or ID, or a Discord ID or @mention. Leave it empty in a DM and the bot checks the person you're talking to (their Discord account).
- `platform` can be Auto-detect (the default), Roblox or Discord. Auto-detect treats mentions and 15+ digit IDs as Discord and everything else as Roblox.
- `visibility` can be Only me (the default) or Everyone in the channel. Errors are always private.
- `everyone:True` checks everyone else in the DM or group DM (up to 10 people) and lists them worst-first, one coloured dot per source. It doesn't work in servers, because a user-installed app can't see a server's member list.

Roblox checks cover all five sources plus Rotector's linked-Discord data. Discord checks cover Rotector, TASE, Okappiki and ServerSweep, because RAB has no Discord lookup.

### Colours

Each source gets its own coloured card, and the header shows a one-line summary:

- 🔴 red: flagged or confirmed (an actual finding)
- 🟠 orange: a process state such as queued, provisional or awaiting review
- 🟢 green: no record in that source (this does not mean the account is safe)
- 🟣 purple: the source errored or didn't answer (this does not mean the account is clean)
- 🔵 blue: Rotector found linked Discord or alt accounts
- ⚪ grey: the source doesn't support this lookup, or nothing was linked

## Setup

1. Install Python 3.10 or newer, then run:
   ```
   python -m venv .venv
   .venv\Scripts\activate        (Windows)
   source .venv/bin/activate     (macOS/Linux)
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill it in:
   - `DISCORD_TOKEN` comes from Developer Portal, then Bot, then Reset Token.
   - `OWNER_ID` is your Discord user ID. Turn on Developer Mode, right-click yourself and choose Copy User ID.
   - `RAYWARD_API_KEY` is your key from https://rayward.app. One key covers all five sources.

## Enable user install (Developer Portal)

1. Open https://discord.com/developers/applications and select your app.
2. Under **Installation**, tick **User Install** in Installation Contexts. You can untick Guild Install.
3. Under **Default Install Settings**, then **User Install**, add the scope `applications.commands`.
4. Copy the **Install Link**, open it and choose **Try It Now** (or "Add to My Apps").

The bot needs no privileged intents.

## Run

```
python bot.py
```

The bot syncs its slash commands globally each time it starts. New commands can take a few minutes to appear. Restarting your Discord client often makes them show up sooner.

## Notes

- Rayward's terms say not to store responses for more than 24 hours. This bot stores nothing.
- "Unflagged" means a source has no record of the account. It does not mean the account is safe. Only Flagged and Confirmed are findings.
- A 503 error means the source did not answer. The bot then shows that source as errored, never as clean.
- Rayward hides parts of Discord IDs in evidence when you use a developer key.
