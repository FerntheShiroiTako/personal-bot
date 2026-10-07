# Personal lookup bot

A private Discord bot for Roblox and Discord lookups across the five Rayward sources: Rotector, TASE, RAB, Okappiki and ServerSweep. It installs to your own Discord account, so its commands work in any server, DM or group DM. Only the account set in `OWNER_ID` can use it.

## Commands

Lookups go through one slash command:

```
/check [roblox] [discord] [visibility]
```

- `roblox` is a Roblox username or user ID. The bot tries an all-digit value as an ID first. If no user has that ID, it tries the value as a username.
- `discord` is a Discord user ID or @mention.
- `visibility` can be Only me (the default) or Everyone in the channel. Errors are always private.

You can fill in either option or both:

- Only `roblox`: checks all five sources plus Rotector's linked-Discord data.
- Only `discord`: checks Rotector, TASE, Okappiki and ServerSweep. RAB has no Discord lookup.
- Both: runs both checks at the same time and sends the Roblox results, then the Discord results, each with its own header. If Rayward links the two accounts, the headers say so and name the source. Otherwise the bot says nothing about a link, since a missing link doesn't prove the accounts are unrelated.
- Neither, in a 1-on-1 DM: checks the person you're talking to, if Discord shares who that is.
- Neither, anywhere else: the bot asks who to check. Group DMs and servers never share their members, so use the right-click commands there.

If either value isn't a valid username, ID or mention, the bot replies with an error and runs neither check.

### Right-click commands

Right-click a user or a message and open **Apps**:

- **Check user** checks that person's Discord account.
- **Check author** checks whoever sent that message.

These work in servers, DMs and group DMs. Discord doesn't allow options on right-click commands, so the results are always private.

### Colours

Each source gets its own card with a coloured side bar, and the card title names the result in words, for example "TASE database - No record". The header lists every source's result on one line in plain text.

- Red: flagged or confirmed, an actual finding.
- Orange: a process state such as queued, provisional or awaiting review.
- Green: no record in that source, which is not the same as safe.
- Purple: the source errored or didn't answer, so its result is unknown.
- Blue: Rotector found linked Discord or alt accounts.
- Grey: the source doesn't support this lookup, or nothing was linked.

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

On startup the bot syncs its slash commands globally, but only if they changed since the last sync. It keeps a small stamp file in `.state/` locally, or in `/var/lib/lookup-bot` on the server. New commands can take a few minutes to appear, and restarting your Discord client often makes them show up sooner.

Optional `.env` settings:

- `LOG_LEVEL` sets how much the bot logs: `DEBUG`, `INFO` (the default), `WARNING` or `ERROR`.
- `SYNC_COMMANDS` controls command syncing. `auto` (the default) syncs only when commands changed, `always` syncs on every start, and `never` skips syncing.
- `DEBUG_INTERACTIONS=1` logs the raw channel data of each interaction. It includes user IDs, so leave it off unless you are debugging.

## Deploy to an Ubuntu VPS

The scripts in `deploy/` run the bot as a systemd service on Ubuntu 22.04 or 24.04. It runs as a locked-down `lookupbot` user from `/opt/lookup-bot`, restarts if it crashes and starts on boot. You need SSH access to the server and an account with sudo.

### First install

Either clone the repo on the server:

```
git clone <your-repo-url> lookup-bot
cd lookup-bot
sudo bash deploy/install.sh
```

Or push your local copy from your own machine. This works from Git Bash on Windows too:

```
bash deploy/push.sh user@your-server
```

`push.sh` copies the code to `~/lookup-bot` on the server. Then it runs `install.sh` there on the first push, or `update.sh` on later pushes. If SSH needs extra options, set `DEPLOY_SSH_OPTS`, for example `DEPLOY_SSH_OPTS="-p 2222 -i ~/.ssh/vps"`. You can also set `DEPLOY_HOST` instead of passing the host.

`install.sh` does the following:

- installs Python and git
- creates the `lookupbot` user
- copies the code to `/opt/lookup-bot`
- builds a virtualenv there
- installs and enables the service

Your local `.env` is never copied. On the first install the script creates `/opt/lookup-bot/.env` from `.env.example` and doesn't start the bot. Fill in the file, then start the bot:

```
sudo nano /opt/lookup-bot/.env
sudo systemctl restart lookup-bot
```

It's safe to run `install.sh` again. It never overwrites an existing `.env`.

### Updating

- On the server, from the clone: `sudo bash deploy/update.sh`. It runs `git pull`, copies the code, reinstalls requirements only if `requirements.txt` changed, and restarts the bot.
- From your machine: `bash deploy/push.sh user@your-server`.

### Logs and status

```
sudo journalctl -u lookup-bot -f        # follow live logs
sudo journalctl -u lookup-bot -n 100    # last 100 lines
sudo systemctl status lookup-bot
```

### Stopping

```
sudo systemctl stop lookup-bot             # stop until the next boot or restart
sudo systemctl disable --now lookup-bot    # stop and don't start on boot
sudo systemctl enable --now lookup-bot     # turn it back on
```

If the token or another `.env` value is missing or invalid, the bot exits with code 78. systemd then leaves it stopped rather than restarting it. Other crashes restart after 10 seconds. If the bot fails 5 times within 5 minutes, systemd gives up until you fix the problem and run `systemctl restart`.

## Notes

- Rayward's terms say not to store responses for more than 24 hours. This bot stores nothing.
- "Unflagged" means a source has no record of the account. Only Flagged and Confirmed are findings.
- A 503 error means the source did not answer. The bot then shows that source as errored, never as clean.
- Rayward hides parts of Discord IDs in evidence when you use a developer key.
