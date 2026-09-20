# Localhost dashboard

`agentlane ui` serves a small read-only dashboard so a person can follow a multi-agent run
without reading logs by hand. It uses the Python standard library only, needs no network access
and ships no external stylesheet, font or script.

```sh
agentlane ui                       # http://127.0.0.1:8787/
agentlane ui --port 9000 --refresh 5
agentlane --board-dir /path/to/board-checkout ui
```

It prints the URL, the board checkout and the worker receipt directory it reads, and stops on
Ctrl-C. Open the printed URL in a browser.

## Views

- **Overview** (`/`): every task grouped by state, with owner and agent, the age of its last
  update, declared paths, and its claim's health: `stale`, time left on the lease, and a warning
  when a live claim has stopped heartbeating.
- **Task detail** (`/task?id=TASK_ID`): description, declared paths, branch, landing history, the
  full claim including lease, base, expiry, heartbeat and extensions used, the note timeline in
  order, the approval records with reviewer, commit, base and evidence, and the worker runs whose
  receipts name that task. An unknown task returns 404 with instructions to refresh the local
  board snapshot and reload.
- **Sessions** (`/sessions`): every local worker run newest first with status, task, start,
  duration, exit code and the literal argv, plus a bounded tail of stdout and stderr for any run
  whose receipt says it is running. `/sessions?run=RUN_ID` tails a specific run. Status comes
  directly from receipts: `running (unverified)` does not confirm a live process, and a crashed
  supervisor may leave that status behind.
- **Activity** (`/activity`): claims, notes, approvals, landings and every other board event from
  all workers merged into one reverse-chronological timeline. This is the page that makes a
  multi-agent run followable. Each page shows up to 300 entries, the total count and page number,
  with **Older** and **Newer** links (`/activity?page=2`). Pages are positions in the current local
  snapshot; concurrent updates may move entries between pages. All records are read and sorted
  for each request; pagination bounds the rendered page, not the memory used to read history.

A missing board directory and a directory without `tasks/` show distinct setup messages naming
the resolved path. An existing empty `tasks/` directory shows an empty snapshot. These checks do
not verify Git branch identity; `--board-dir` must point to the intended board checkout.

Pages refresh themselves with a `<meta http-equiv="refresh">` tag; `--refresh 0` turns that off.

## What it will not do

- **Read-only.** No endpoint takes, releases, extends, approves, lands or otherwise changes the
  board, and the server answers `GET` only. Use the CLI for anything that changes state.
- **Loopback only.** There is no authentication, so the bind is the security boundary.
  `--host` accepts `127.0.0.1`, `::1` or `localhost`; anything that resolves to a non-loopback
  address is refused before the socket is opened. IPv4-mapped IPv6 addresses such as
  `::ffff:127.0.0.1` are also refused with a message directing you to the supported forms.
- **Inert output.** Task titles, notes, approval evidence, worker argv and log text are written by
  agents and treated as hostile: everything is HTML-escaped and control characters, including the
  ESC that begins a terminal escape sequence, are replaced before output.
- **Bounded logs.** Worker stdout can reach tens of megabytes. Only the last 64 KiB of a log is
  read, and the page says the view is truncated and how large the file is.
- **No fetching.** The dashboard shows the board checkout exactly as the last AgentLane command
  left it; it never fetches, never writes and never takes the checkout lock. Run `agentlane
  status` in the project clone to refresh its default board checkout, and the next page load
  shows the new state. With `--board-dir`, ensure that path names the refreshed checkout.
- **No worker lock probes.** Session reads validate local receipts without taking the worker
  launch lock or opening another clone's marker. They do not establish process liveness.
- It reads only the board checkout and `agentlane-workers/` under this coordinator's Git common
  directory, and a run's log only from inside that run's own receipt directory: a receipt naming a
  path outside it is refused on the page. The resolved, checked path is also the path read.
  This path check is not an atomic filesystem boundary: another local process replacing a file,
  directory, symlink or junction during a read can still race it. Board records can likewise
  change between reads, so a page may mix states during an update. Use trusted local checkouts
  and receipt directories. Worker logs can contain secrets, one more reason to keep the bind local.
