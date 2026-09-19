# Shared interactive UI: delivery plan

Proposal for [issue #9](https://github.com/thetrueshags/AgentLane/issues/9).
This document specifies future behavior; it does not announce a shipped service.
Central means multiple authorized people using one authenticated service and one
authoritative project board. A localhost dashboard, shared folder, or private
forwarded port is useful local tooling but does not satisfy that outcome.

## Evidence and source provenance

Audited 2026-09-19 against upstream main
`fbb5fce2df8f9088c06b1ff77f4de9b88f813c9e`, freshly fetched from
`https://github.com/thetrueshags/AgentLane.git`. This is the source repository,
not automatically the remote of a project using AgentLane.

| Evidence | Behavior at audited revisions |
| --- | --- |
| [README](../README.md), [architecture](architecture.md), `agentlane/board.py` | CLI/MCP board on configured Git remote; transactional board writes; no central HTTP service on main. |
| `.harness/config.json`, `Repo`, `Board.sync/txn` | Defaults: `origin`, `main`, `board`; direct landing; review opt-in false; 45-minute TTL and 15-minute inactivity release. Project configuration can override them. |
| Fresh source `python -m agentlane --help` and `--json doctor` | No `ui` command; remote main reachable, board/member/hook absent. These are expected for source maintenance, not instructions to initialize AgentLane's own board. |
| [workers](workers.md), `agentlane/worker.py` | Local supervisor receipts/logs under coordinator Git common directory; run name does not join a board; task association is unverified. |
| `feature/localhost-ui` at `a2b0f2ace6537647b4fda5c1fd90a8d74bf7fcec` | Separate unmerged candidate: `agentlane/ui.py`, `docs/ui.md`, `tests/test_ui.py`; loopback GET-only dashboard, automatic page reload, no Git fetch. |
| Retained independent review of that exact UI candidate | Round-two containment fix accepted; concerns at that baseline included concurrent mixed board reads, worker-lock probes, and log path check/read race. Historical review is not validation of a shared service. |

The localhost prerequisite is tracked in [PR #11](https://github.com/thetrueshags/AgentLane/pull/11).
The reviewed baseline `a2b0f2ace6537647b4fda5c1fd90a8d74bf7fcec` was republished as
`5ef6e43aaad8a18f00afa7955031e17a8f205ecc` with corrected attribution metadata only;
the source tree is identical. As of 2026-09-20, the feature head is
`688f4a82f5dc7c4ae3ebad641cb245018cd12da6`; its subsequent changes are outside this
historical baseline audit.

The audited UI baseline is 787 added lines over audited main. Its renderer and escaping
tests are reusable after independent upstream review; its HTTP/session/log boundary
is not a shared-service foundation to expose unchanged. Do not broaden its bind or
proxy its log routes onto a shared address. Local-dashboard residual maintenance
is separate backlog, not the shared UI design or a claim that auth/sync exists.
Do not depend on unpublished customized checkouts or private launch scripts.

Local disposable real-Git exploration used a new bare remote and tiny project:
missing-board status failed without initialization; explicit fixture initialization
succeeded; two vanilla clones joined with distinct local identities; a task created
in one appeared through `show` in the other; repeat initialization was refused.
No existing board was mutated. Source audit and CLI probes establish the baseline;
no shared-service deployment or two-browser acceptance has yet been performed.

## Journeys and acceptance

| Person / workflow | Required observable result |
| --- | --- |
| Project administrator: first setup | Doctor distinguishes unreachable remote, absent board, absent local membership, and absent UI service. Initialize only the intended new project's board, once. Publish setup files through the project's normal review/landing workflow. |
| Worker: join existing project | Clone the exact project remote, verify branch settings, join with a unique worker name, and register this clone for optional status publication. No copied `.git`, member file, private scripts, or second board. |
| Owner: open shared URL | Authenticate; see project identity, board/main revisions, successful fetch time and connection health; inspect tasks, claims, review evidence and activity. Reload requires no manual CLI refresh. |
| Owner: give direction | Add a task note with a preview of what becomes shared Git history. Another browser and a fresh CLI read see one attributed note and its commit receipt. Note does not approve or complete work. |
| Observer: follow workers | Distinguish member, claim heartbeat, reported process status, and reporter connectivity; absent reporting is explicitly unavailable, never implicitly healthy. |
| Operator: outage/restart | Stale timestamps remain visible; unavailable writes fail closed; restart restores project mappings/audit receipts but requires fresh fetch and fresh worker reports before green status. |

Completion requires independent clean-clone setup plus two separately authenticated
browser sessions against the same service, including a second client machine on
the authorized network. Two tabs on localhost are development evidence only.
CLI-only usage remains supported with no central-service dependency.

## Onboarding contract

Separate installing the AgentLane tool, setting up a project, joining a worker,
and connecting a browser. Current source use needs Python 3.9+, Git and Bash;
`python /path/to/AgentLane/bin/board ...` can exercise the CLI without installing.
Use the documented local-source/wheel install for normal deployment; do not imply
a public package release or a shared UI command already exists.

1. Administrator records a project descriptor: stable project ID, credential-free
   canonical Git URL, selected remote alias, exact main/board branch names, shared
   UI URL, and supported AgentLane revision. The authenticated service exposes the
   non-secret descriptor; committed project docs may link to it. Runtime secrets
   and machine-specific paths remain outside tracked project configuration.
2. Resolve `.harness/config.json`'s remote with `git remote get-url <alias>` and
   `git remote get-url --push <alias>`; compare both with the descriptor. `origin`
   alone proves nothing: it may be a fork or local mirror. Configure the authoritative
   remote explicitly when different. Reject mismatched or ambiguous endpoints;
   administrator-approved SSH/HTTPS equivalents need an explicit mapping.
3. Check exact refs with `git ls-remote --heads <alias> <main> <board>` and run
   `agentlane doctor`. Network/auth failures must not offer to create a new board.
   Missing remote main needs an initial commit. If the board is truly absent, only
   the project administrator follows README's `install --project`, `init`, setup
   lane and gate customization steps. Service never initializes a board implicitly.
4. Existing-project workers clone that project, read its instructions, run doctor,
   check the requested name is unused, then run
   `agentlane join --name <unique-worker> --agent <tool>` and `agentlane status`.
   Do not reinitialize, infer identity from Git user.name, or reuse another worker's
   `.harness/member.json`. Current `join` can overwrite a same-name member record;
   service registration must detect collisions rather than treating it as auth.
5. New service registration binds an authenticated principal to a board member,
   opaque host ID and stable clone ID generated locally. Verify remote/refs and
   membership, require administrator authorization of the mapping, and reject a
   duplicate active clone identity. Several clones for one person need distinct
   worker identities. A board membership alone does not register a reporting host.
6. Show a registration result and a first-report check. Unsupervised workers can
   join and work normally; UI says "process telemetry unavailable" until explicitly
   connected. Browser access requires no local clone or local filesystem access.

No existing service descriptor or runtime registration mechanism ships on audited
main. Implementation should document exact new commands/options with the corresponding
slice and validate them from a clone containing only tracked project files.

## Smallest useful service

Use one optional AgentLane process for one configured project on an existing host,
with a dedicated service clone and an authenticated HTTPS entry point. Retain Git
as the task/claim/review authority. No scheduler, cloud control plane, message bus,
separate task database, agent execution API, or multi-project tenancy in this release.
Use simple server-rendered pages/forms and bounded polling; no frontend build chain
is necessary. Keep dependency additions explicit and reviewed if ingress needs them.

Suggested first PR: a private fetcher producing a validated, immutable board
snapshot with revision/time/error metadata and tests. It exposes no network server.
The first useful interactive milestone combines that reader with authenticated
task/activity pages and one operation: append a note. Follow it with shared worker
metadata and verified central operation; do not describe the reader milestone as
delivery of the full outcome.

Browser controls in the first interactive milestone:

| Capability | Authorization and execution |
| --- | --- |
| Read tasks, notes, events, claims, review evidence | Project reader; escaped text, pagination/bounds, no raw filesystem or log routes. |
| Append note | Project planner mapped to a distinct registered member; validated task/text; core board transaction; durable authenticated attribution and idempotency. |
| Refresh / filter | Reader; triggers or observes bounded snapshot refresh, never heartbeat, expiry sweep or review reconciliation. |
| Create backlog task (later small slice) | Planner; existing `add` validation/transaction; no claim, assignment or branch switch; same audit/idempotency contract. |

No web take/release/extend, approval/withdrawal, landing/merge, config editing, or
worker start/stop/restart in this scope. Display copyable CLI guidance in context.
Claims, hot paths, leases, target-main review policy, exact candidate/base approvals,
gate execution, and atomic landing stay in the existing engine and worker clones.
UI must label approval evidence as recorded, withdrawn, outdated, or applicability
unknown; it cannot equate a note, process exit, or old approval with permission to land.
PR submission remains review, not completion; `sync --reviews` stays explicit.

## Synchronization and failure semantics

Fetcher owns its service clone; it never resets a worker checkout or scans a shared
directory as a substitute for remote synchronization. Fetch configured main/board
every 5 seconds, bound each attempt to 10 seconds, and atomically swap a fully
validated snapshot built from exact Git objects. Read all records at one board SHA,
not a mutable worktree during `Board.sync`. Record fetched main SHA separately;
the two refs are not claimed to be an atomic read or an approval-validity proof.
Use project timing/review settings from fetched main, not a worker's candidate;
show that policy revision alongside claim health. Missing/invalid policy fails closed.

Browser polling every 5 seconds targets visibility within 15 seconds of a remote
commit under healthy fixture/network conditions. Measure this with two clients.
Every response includes board SHA, main SHA, last successful fetch, last attempted
fetch and sanitized error; fetching an unchanged SHA still advances fetch evidence.
Failed fetch marks degraded immediately; more than 30 seconds without success marks
stale. Initial failure has no data, not an empty board. Corrupt/incompatible records
retain last valid snapshot with error and age; never silently omit broken tasks.

Browser tracks last successful service response independently. After 15 seconds
without one, display disconnected and disable writes even if the cached page was
healthy. Reconnection requires fresh server status. Server restart begins "checking"
and disables writes until Git and identity dependencies are verified. No offline
write queue or optimistic "saved" message.

Each write freshly synchronizes, checks project authorization and the target task,
and uses the existing bounded board transaction/retry mechanism. Serialize service
writes in its own clone; concurrent CLI writers still use Git compare-and-swap.
Carry request ID and authenticated actor in an additive, validated board event
committed with the note. Deduplicate against fetched board events within every retry,
including restart. Same request ID with different content is rejected. Do not wrap
an unmodified CLI note command and append audit metadata in a second transaction.

Report success only after remote confirmation with resulting board SHA. A lost push
response is "outcome unknown": query by request ID after fetching, then show committed
or safely retry the same request. Never invent failure/success or duplicate notes.
Refresh snapshots after confirmed writes. Disabling all writes when Git/auth/audit
is unavailable is acceptable; hiding uncertainty is not.

## Identity, audit and browser safety

Use an existing operator-managed authenticated HTTPS reverse proxy if one is
available. Select one supported identity-header contract in the ingress slice;
trust it only from that proxy over a restricted local connection. Strip incoming
identity headers at ingress, reject direct/backend access and unexpected Host or
Origin, and maintain server-side principal-to-project-role/member mappings.
The contract must include verified stable subject and session identity/expiry so
CSRF tokens bind to one login session and revocation cannot leave a writable session.
Fail startup of shared mode without a validated auth configuration. A LAN address
or possession of a board member name is not authentication. Do not implement a new
password store or assume an identity provider already exists.

All writes require POST, authorization checked per request, a session-bound CSRF
token and exact-origin validation. Proxy session cookies must be Secure, HttpOnly
and suitably SameSite; logout/revocation must stop writes. No mutation via GET, no
wildcard CORS, and no authentication tokens in URLs. Bound request size/time/rate,
escape all agent-authored text, constrain links, and apply a restrictive CSP plus
private/no-store response caching. Test replay, injection and proxy bypass.

Audit accepted writes in the same Git commit: stable actor ID, mapped member,
project/task, action, UTC time, request ID and prior revision; return result SHA.
Keep denied attempts and service lifecycle/security events in restricted rotating
operator logs, excluding credentials, cookies and note bodies. If required audit
cannot be durably recorded, reject writes. Disclose shared Git retention before
posting notes; removal from a page does not erase Git history.

Provision a dedicated project-scoped service Git identity for board notes; give it
no main-push/merge authority where host permissions support that split. If the host
cannot separate branch rights, document residual cooperative access and require
protected main plus reviewed service restrictions before enabling writes. Git writers
can bypass cooperative CLI policy; this UI must not claim adversarial enforcement.

## Worker and log boundary; ownership of status and restart

Task claims come from the Git board. Local process evidence comes only from an
explicitly registered publisher on the supervisor's host; central code must not
probe another host's PIDs, lock files or mounted `.git` trees. First integration can
use one same-host publisher; the same authenticated publication contract supports
a later second host without changing the meaning of a running claim.

Publish an allowlist only: project/member/host/clone/run IDs, optional task ID,
supervisor-observed state, start/finish time, observed exit code, report sequence
and service receipt time. A supplied task ID remains unverified unless it matches
the board claim's owner/branch/lease. Exclude argv, prompts, environment, local paths,
stdout/stderr, receipts as blobs, and provider/session transcripts. No log-tail API
or filesystem download in shared mode. Keep opt-in publication and local logs local
by default; board notes/review evidence are already shared and still need access control.

Reporter sends every 10 seconds; >30 seconds without a service-received report
becomes "reporter offline; last observed running/exited/...". Reported process state
and connectivity are separate. A disconnected reporter cannot prove process death.
Only supervisor evidence establishes exited/failed/interrupted; uncertain cleanup
remains unknown. Persist a session epoch and monotonic sequence to reject replay;
use receive time for freshness and expose clock skew rather than trusting host time.
On service restart retain last evidence as stale until newly authenticated reports.
On publisher restart issue a new epoch and reconcile locally before reporting running.

The host operator owns service/publisher start, stop and restart through an existing
OS service manager outside foreground worker containment. Worker owners retain
explicit local orphan inspection and `worker resolve --acknowledge-stopped`; service
restart must never restart workers, clear markers or renew task claims. Admin-only
registration revocation stops reports; it does not stop the underlying process.
Keep mappings and latest status in protected, atomically replaced service-local
records; bound retention and expose its policy. Do not commit every worker heartbeat
to the board. Git remains authoritative for tasks; status records are observations.

## Bounded delivery and independent QA

These rows suggest focused PR boundaries, not additional contribution rules.
Follow [CONTRIBUTING.md](../CONTRIBUTING.md) for scope, validation and review;
split a slice further when that makes the change easier to review.

| Slice / dependency | Deliverable and independent acceptance |
| --- | --- |
| S1: main | Dedicated immutable Git reader, freshness/error model. Two fixture clones mutate remotely; fetcher observes without CLI refresh; mixed snapshots impossible; unreachable/corrupt remote retains labeled old data. No listener. |
| S2: S1 | Authenticated read pages and descriptor; proxy trust contract and health endpoint. Clean clone checks exact remote/refs; two principals see same SHA; anonymous/forged headers/backend bypass refused; injection inert; missing board differs from empty. |
| S3a: S1 | Core transaction adapter for attributed idempotent notes, compatible event validation. Concurrent CLI writes, retries and crash-after-push yield exactly one note/event; no second audit commit. |
| S3b: S2 + S3a | Note form, roles, CSRF and outcome receipts. Reader denied; planner accepted; cross-origin/missing-token requests denied; revocation effective; unknown push reconciles; no gate/approval mutation. First interactive milestone. |
| S4a: S2 | Principal/member/host/clone registration and allowlisted status ingest. Collision, wrong project, unregistered host, replay and revoked publisher refused; no private payload accepted. |
| S4b: S4a | Optional local publisher and workers page. Real harmless foreground worker exits/fails; hard-killed supervisor yields unknown; report loss ages offline; claim heartbeat never substitutes for process health. |
| S5: S3b + S4b | Runbook and existing-host pilot. Independent clean-clone and second-machine browser exercise; service/remote/reporter outage and restart; operator ownership and private-data boundary checked. |
| S6: after S5 | Optional task creation, using same transaction/audit controls. Open task visible in both clients, no claim or branch switch. Defer further controls until evidence requires them. |

QA records exact candidate/base/runtime, fixture remote/refs, browser identities,
timestamps, observed SHAs/request IDs, outcomes and cleanup. Use synthetic notes,
fake credentials and dummy worker output. Never copy private receipts/logs into PRs.
Retained localhost review is supporting evidence only; shared auth needs new tests.

Cross-cutting matrix: run Python 3.9/3.12 Linux and supported Windows checks; exercise
missing/mismatched remote, duplicate identity, offline/corrupt board, changed review
base/lease, expired claim, concurrent write, restart and revoked session. Assert UI
cannot approve or land, and existing gate/atomic-landing/review tests still pass.
Use two real browsers for CSRF/session and freshness acceptance, not just HTML fetches.
Record unavailable OS/browser cases as deferred, never passed. Review exact pushed
candidate in a fresh checkout; upstream issue/PR discussion owns design acceptance.

## Existing-resource deployment decision

Preferred pilot: one already available always-on team machine, dedicated service
clone/storage, and existing HTTPS/auth ingress. This needs no new hosted platform;
reuse the team's Git host for authority. Confirm uptime, reachability from a second
machine, ingress ownership, branch permissions and protected local storage first.
An existing managed internal server/VM is the fallback if the workstation sleeps or
cannot accept authorized clients; use the same single-process design and runbook.

If neither has authenticated ingress, use loopback-only synthetic development
until an operator selects and configures a suitable host and authentication setup.
Do not tunnel/expose the current unauthenticated dashboard. Existing resources
should suffice for a pilot; paid hosting is not a prerequisite.

Unresolved deployment inputs: machine, operator, internal DNS/TLS endpoint, existing
identity proxy, permitted client network, service account/branch permissions, backup
and retention policy. Operators should inventory these inputs before selecting a
deployment target. This source audit does not establish host uptime or ingress availability.

Runbook must give version-pinned startup/config checks, service status and health
interpretation, safe stop/restart, log rotation, descriptor/mapping/audit backup and
restore, revocation, and rollback to read-only/offline with CLI unaffected. Health
reports process readiness, Git freshness, auth readiness and publisher coverage
separately. Success means independently verified central operation plus reviewed
upstream contributions, not this plan or a pushed feature branch alone.
