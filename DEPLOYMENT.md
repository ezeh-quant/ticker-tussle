# Ticker Tussle: Forecast Arena

## Local play

From the project folder, run `python outputs/server.py` and open `http://127.0.0.1:8765/`. Keep the PowerShell window open while playing. If the older server is still running, press Ctrl+C in its window before starting the updated server. Python's standard library is enough; no package installation is needed.

## Publish a public demo with Render

1. Put the **whole project folder** in a GitHub repository. Keep `render.yaml` at the repository root and the game files in `outputs/`.
2. In Render, choose **New → Blueprint** and connect that repository. The included blueprint selects a free Python web service and starts it with `python outputs/server.py`.
3. Wait for deployment and copy the public HTTPS address Render gives the service. A `127.0.0.1` address is local and cannot be submitted as the project link.
4. Follow the two-device check below before sharing the link.

The included free service is suitable for a demo, with limits: it can sleep after 15 minutes without visitors, and its local SQLite file may be erased on a restart or spin-down. That clears active rooms and scores. Render says free web services have ephemeral filesystems and can take about a minute to wake after idle: https://render.com/docs/free. For persistent games, use a host with persistent storage or a managed database. Do not choose a paid plan without checking its cost first.

## Two-device check before sharing

1. Open the public link on one device and create a room. Copy the invite link.
2. Open the invite link on a second device/browser, enter a different name, and join. Check that both players appear in the same room.
3. From the host screen, start the game. Both screens should show the same round number, starting BTC/USD price, and countdown.
4. Lock different forecasts from the two devices. Confirm the other player's exact forecast remains hidden until resolution.
5. At the deadline, confirm both players see the same actual BTC/USD quote, the revealed forecasts, and the updated shared leaderboard.
6. Send chat messages from each device and confirm they appear only in that room. Try a second room to confirm rooms are separate.
7. Try the 30-second, 1-minute, and 2-minute settings in separate new rooms. Check a phone-sized screen and refresh a joined browser to confirm it rejoins the same player session.
8. If something fails, check the Render service logs and `/api/health`; for a market quote problem, retry after CoinGecko responds.

## Current rules

- Up to 12 players join a six-character room code. The host needs at least one other connected player to start.
- The host chooses a 30-second, 1-minute, or 2-minute timer before the match. It stays fixed for all 20 rounds.
- The server stores each player's forecast privately and accepts one call per round before its deadline.
- At the deadline, the server fetches the CoinGecko BTC/USD spot quote and uses that same actual value for all submitted forecasts.
- Score: `max(0, 100 - 20 × absolute percentage error)` per round. A player who misses a round receives no points for that round.
- Room chat, player roster, timer, reveals, and cumulative leaderboard are shared only within that room.

Players do not need an account or app install. This project is a market forecasting game, not a trading product or financial advice.