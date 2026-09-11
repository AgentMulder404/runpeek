# Shared work accounting — private pilot

RunPeek 0.3 adds a metadata ledger, orchestration SDK and optional self-hosted receiver.
It is not a deployed SaaS or a universal reader for ChatGPT/Claude browser conversations.
No model calls are made for collection, pricing, reconciliation or reports.

## Local setup

Install `pip install -e .` from this checkout, then in the project you work on:

```sh
runpeek init
runpeek collect
runpeek status
```

`init` detects the supported local agent records, explains collection scope and asks once
before collection. Name a work item and optionally attach the latest root session; older
sessions remain unassigned. Local adapter storage includes paths/tool labels and keyed
fingerprints, not prompt/response text. There is no upload during setup.

`collect` refreshes both agents, attaches newly discovered root sessions to the active
work item and bridges assigned usage to the shared ledger. An active work item is an
explicit routing choice, not inferred intent. Use `activate` when switching tasks; sessions
that span several tasks still require session-level attribution, not automatic splitting.

```sh
runpeek activate wi-123456
runpeek join AUTH-42 --name 'Authentication feature'
runpeek work assign AUTH-42 SESSION_ID
runpeek ledger report --work-item AUTH-42
runpeek pause
runpeek resume
```

On the second device, `join AUTH-42` uses the exact same ID. IDs may be shared without
sharing local work names or repository paths. Pause affects subsequent collector/SDK
persistence and sync; already-running writes can finish. SDK usage dropped while paused
is reported in the SDK counters and is not recovered automatically.

## Orchestrator integration

```python
from runpeek.tracking import Tracker, task

with Tracker('.runpeek/runpeek.db', agent='my-agent', source='my-orchestrator') as tracker:
    with task('AUTH-42'):
        # Run your existing provider call here. No prompt or result text is sent to RunPeek.
        tracker.record(
            provider='openai', model='gpt-5', request_id='provider-response-id',
            input_tokens=1000, cache_read_tokens=500, output_tokens=100,
        )
    counters = tracker.close()
    print(counters)
```

Supply real measured fields, not the example values. Input means **uncached input**;
output includes reasoning. Pass `account_scope` consistently for the same provider
account. Automatic estimates use existing dated rate cards; cache-write usage without an
explicit TTL-aware amount stays unknown. Supply actual charges or allocation amounts
explicitly with `basis='actual'` or `basis='allocated'` and integer USD `amount_nanos`.
Only USD is supported; imports in other currencies must be converted under a documented
policy before ingestion. No exchange rate lookup is performed.

Python async tasks inherit context. Pass `Tracker.context()` explicitly to other threads,
processes or services, then use `context=...` on `record`. Subagents use `task(...,
parent_session_id=...)`. The SDK does not intercept raw HTTP or arbitrary agent frameworks:
the integration point is the model call completion. Calls without a work context are
rejected and counted. Never propagate unrelated application headers as tracking context.

Queueing is bounded and non-blocking. Inspect `close()` counters for dropped, invalid,
failed and unflushed events. Local disk failure must not fail the model request. Committed
observations form the durable sync queue. Uncommitted in-memory events can be lost on crash.
Run `python examples/benchmark_tracking.py` to measure overhead on your machine.

## Reconciliation and corrections

Observations are immutable and keyed by `(workspace, source, event_id)`. Reusing an event
identity with different data rejects the entire batch. Matching provider/account/request
identities form one charge. Actual amounts replace estimates; conflicting amounts at the
same priority, or conflicting work assignments, are excluded and counted visibly.
Without request identity, only same-source deduplication is possible. Missing cross-source
identity is reported; the system does not claim those amounts are globally deduplicated.

Allocation is explicit and must cover non-overlapping activity. RunPeek cannot determine
whether a manually entered monthly subscription allocation overlaps another manual entry.
The headline total uses the stated policy, and always breaks out actual, estimated and
allocated amounts. It is not necessarily the provider bill or complete cost of a task.

Local work reassignment creates a ledger attribution override without rewriting evidence.
Use `runpeek ledger assign --charge-key HASH --work-item AUTH-42` to resolve a conflict.
Charge keys can be calculated with `runpeek.ledger.charge_key(observation)` from a ledger
export. The hub operator can use `runpeek hub assign` with the same flags and `--workspace`.
Sync carries local overrides only for charges the device has submitted. Another device's
concurrent explicit correction is last-write-wins, recorded as an audit event; there is no
collaborative conflict UI in this pilot.

## Other platforms and imports

ChatGPT web, Claude web, local inference, other CLIs and billing systems are supported only
when you supply accounting observations via the SDK or strict JSONL import. No browser
scraping, password collection or claim of inaccessible token telemetry is built in.
Use `runpeek ledger export --file observations.jsonl` to see the supported schema.
`runpeek ledger import --file observations.jsonl` validates each bounded line; valid earlier
lines remain committed if a later line is invalid. Reimport is safe. Extra fields (including
prompts, arguments and workspace IDs) are rejected. Identifier fields are metadata, not a
channel for content; user-supplied values can still be sensitive.

## Optional receiver and device connection

For a two-device synthetic acceptance demo without model calls or external network:

```sh
python examples/unified_demo.py
```

A private local receiver:

```sh
runpeek hub serve --db /private/path/hub.db
```

For remote use, provision an encrypted persistent disk and supply a trusted TLS certificate
and private key. Example (replace paths; no public deployment happens automatically):

```sh
runpeek hub serve --db /encrypted/runpeek/hub.db --host 0.0.0.0 \
  --cert /private/tls/fullchain.pem --key /private/tls/privkey.pem
```

The built-in server is a bounded single-process private-pilot receiver. Do not expose it as
a production multi-tenant SaaS: a production gateway with connection limits, operational
monitoring, managed encrypted storage/backups, certificate renewal, independent security
review and load testing is still required. OS file permissions are not encryption.
Local users should use OS full-disk encryption as well. On Windows, restrict the
containing directory ACL; POSIX mode bits alone do not establish an equivalent ACL. Do not place databases on public or
shared-readable folders. All local hub administration relies on trusted OS access.

On a client:

```sh
pip install -e '.[sync]'
runpeek connect https://your-private-host:8765
```

The client explains the upload fields, requires approval, opens the pairing instructions
in a browser and displays an expiring device code. The hub operator verifies the requesting
device, then runs this locally on the hub:

```sh
runpeek hub approve DEVICE_CODE --workspace team-a --db /encrypted/runpeek/hub.db
```

Credentials are placed in the client's OS credential store (macOS Keychain, Windows,
Secret Service or KWallet); plaintext fallback keyrings are refused. The receiver stores
only SHA-256 credential hashes. Device credentials expire after 90 days and can be revoked.
This is device authorization with an operator, **not hosted browser SSO**. No identity
provider or public service has been configured. Pairing itself uploads no accounting data.

```sh
runpeek sync
runpeek hub report --workspace team-a --db /encrypted/runpeek/hub.db
runpeek hub devices --workspace team-a --db /encrypted/runpeek/hub.db
runpeek hub revoke --device DEVICE_ID --workspace team-a --db /encrypted/runpeek/hub.db
runpeek disconnect
```

Sync uses bounded batches, durable acknowledgement receipts and exponential retry backoff.
It ignores ambient HTTP proxies and refuses redirects to avoid forwarding credentials.
HTTPS is required except on loopback. Collectors can ingest and correct their own observed
charges, but cannot query workspace reports or delete workspace data via their credential.
Workspace identity is derived from the authenticated device, never the payload.

## Retention, export and deletion

`ledger export` writes an owner-readable, new-only metadata file. `ledger delete --before
ISO_TIMESTAMP` removes local ledger charges received before a cutoff, retaining hashed replay
tombstones to prevent resynchronization from resurrecting them. Without `--before`, all
ledger observations are deleted after confirmation. This does not delete agent transcripts,
legacy adapter tables, work assignments, or separately stored remote data. Delete the local
store after stopping collectors when you want to remove all local RunPeek data. OS backups
must be managed separately.

Hub operators have `hub export`, `hub delete --before ...`, and `hub delete` per workspace.
There is no automated retention scheduler in this pilot. Audit events store action/counts,
not bodies or credentials. Deletion removes logical records; encrypted-volume key lifecycle,
SQLite free pages and backup expiry are deployment responsibilities. Tombstones and audit
metadata remain until the store is destroyed. Revoke devices before destroying a workspace
store to avoid unintended re-creation elsewhere.

## Legacy no-ordinal repair

The old memory-address IDs affect only Codex records without source ordinals. Ingestion
now refuses those sessions until repaired; ordinary records with ordinals are unaffected.
Stop other collectors and run `runpeek repair --db PATH`. It backs up the original store,
requires every recorded source file, rebuilds derived agent tables in a temporary store,
and replaces them in a transaction. Work items, explicit assignments and session labels
are preserved. Failure leaves the old tables intact. Findings are recomputed during recovery; shared-ledger observations are immutable and are not silently repriced.

If a subagent copied-prefix boundary cannot be determined because source ordinals are
missing, its usage is excluded and marked ambiguous. A cumulative reset is not evidence
that copied parent usage should be billed to the child.

Security baseline: [OWASP REST Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html).
This is design guidance, not a certification or a claim that tests prove production security.
