# Player acceptance boundaries

Production metadata checks and playback acceptance have separate entry points.

`node scripts/verify_unified_player_live.cjs` performs bounded GET requests to a
loopback origin (default `http://127.0.0.1:4871`). It starts no browser and does
not play, seek, mark watched, refresh viewing leases, or submit progress. Set
`PLAYER_QA_OUT` to the receipt directory. It checks metadata, not playback.
`python scripts/deploy_unified_player.py --output <directory>` is a legacy
name for the same read-only verification. It never reloads a process, waits for
idle, or opens the production database. Backend reloads use the owner's separate
reviewed supervisor procedure.
`scripts/deploy_bahamut_alignment.py` likewise performs one GET capability check
with a 1–30 second timeout; missing capability returns a nonzero result for the
owner, never an automatic reload. Its old idle-helper import is retired.

For playback use the project Python environment:

```
python scripts/run_player_qa.py --output <private-artifact-directory> --vendor-root <local-static-vendor-directory>
```

Node, Playwright, an installed Edge channel, and the project ffmpeg tool are
required. The runner creates synthetic video, a new random fixture directory,
and a fresh DB, on port 4879. The nonce-bound fixture refuses unsafe roots,
existing/linked databases, and enabled workers before importing the app. Network
dependencies are fake; the optional vendor directory supplies static assets
only. No existing show, media file, production DB, owner, or task is used.
Current and legacy backend behavior run against this fixture; reset statements
apply only to its new DB. Cleanup stops only the subprocess tree created by the
runner. The fixture is a local test tool, never a deployed application route.

The browser guard blocks Service Workers, WebSockets, non-allowlisted reads,
cross-origin requests, and all writes in read-only mode. A second HTTP proxy
remains active until browser exit, including late pagehide/Beacon requests and
APIRequestContext traffic. Fixture POSTs require a verified nonce handshake;
only that fixture proxy can add the nonce to a late keepalive request. This is
an acceptance boundary for these tools, not an OS sandbox for arbitrary code.

Run `node --test tests/*.test.cjs` with `PLAYER_QA_BROWSER_TESTS=1` on the owner
machine for the real browser negative test. `PLAYER_QA_GUARD_PROOF` optionally
records a synthetic canary receipt after browser shutdown. This canary test
does not send attack requests to production. Python unsafe-setup tests verify
failure before app import. Keep receipts and screenshots locally; do not export
DB copies, request credentials, or private media. Before/after production hashes
support the read-only check but are not proof that unrelated worker/user
activity stopped.
