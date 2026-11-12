# Email collaboration (development fork)

This extension runs **inside the existing Hermes gateway on the same host**. It
uses an existing IMAP/SMTP mailbox and existing chat integrations. It does not
need Docker, a new VM, an additional agent framework or a hosted email service.

## Current delivery boundary

The software can be staged before credentials exist. That is not a live mailbox
connection. Enter credentials locally, then perform a controlled activation and
real external acceptance test before using it with collaborators.

Development version: `0.6.0.dev3`, derived from Email++ upstream commit
`066eb183e71564098e617f63c8062a342d54eb6c`, MIT licensed. Preserve the original
license and attribution. The upstream PyPI release and its release badges are
not this collaboration build.

## Secure credential entry

Once the directory plugin is installed and enabled, run on the Hermes host as
its normal OS user:

```console
hermes email-collaboration
```

This is a local terminal wizard. It asks for the From alias, mailbox login,
server names, dedicated folder, allowed owner aliases, trusted receiving
`authserv-id`, private chat target and owner user ID. The mailbox password uses
hidden `getpass` input. **Never paste the password into an agent conversation.**
Namecheap Private Email defaults are offered for the server names:
`mail.privateemail.com`, IMAP TLS 993 and SMTP STARTTLS 587. Enable the provider's
third-party client protocols and use its supported application password.

An alias cannot log in. For example, the login may be `notifications@example.com`
while the displayed From is `Hermes Agent <hermes@example.com>`. The provider must
permit sending from that alias. Credentials grant access to the underlying
mailbox even if Hermes watches only its dedicated folder.

The wizard atomically updates only `EMAIL_PP_*` entries in the active profile's
owner-only `.env`, preserving unrelated entries. It first disables the email
platform with the supported Hermes config command. **Saving credentials does
not enable email, restart Hermes or send a test message.**

Offline field check, containing names rather than secret values:

```console
hermes email-collaboration --check
```

`credentials_required` means fields are missing/invalid;
`configured_activation_pending` means fields are saved, not authenticated or
live. The check does not test the mailbox or confirm the current gateway loaded
the plugin. The module fallback is `python -m hermes_email_pp.setup` from an
environment where this package is available.

### Receiving-server authentication pin

Use the stable identity before the first semicolon in the topmost trusted
`Authentication-Results` header from a real received message. Do not guess it
from the IMAP hostname. The receiving provider must strip inbound headers that
falsely claim its own identity before stamping its result. Pinning a string is
not independent proof of header provenance.

The collaboration path requires an explicit sender allowlist and authenticated
From alignment. It accepts only the topmost pinned result, not a passing header
buried underneath a failure or a hostname-prefix match. It deliberately uses
strict domain equality for the supported DMARC/SPF/DKIM results. Forwarders or
unusual provider headers may require investigation; do not fix that by turning
sender authentication off.

### Mail routing

Create a dedicated folder such as `Hermes` and a provider-side rule for mail to
the Hermes alias. Keep existing notification traffic out of that folder. Poll
with exactly one adapter; do not enable stock `email` and `email_pp` against the
same messages. Collaboration does not delete messages. It fetches with PEEK so
a crash before durable intake does not make new mail unreadable to recovery.
The wizard selects recovery of unread history; request keys prevent already
accepted messages from becoming new work. Review existing folder contents
before first activation.

## Collaboration behavior

1. An authenticated owner alias explicitly addresses Hermes in the current
   message, for example `Hey Hermes, summarize the GPU tests from yesterday.`
2. The request and recipient snapshot are durably stored.
3. A private acknowledgment identifies the email subject and request ID.
4. Hermes retrieves the specifically requested evidence. Final answers are sent
   as reply-all to the original visible To/CC participants, excluding its own
   mailbox/alias addresses. References and In-Reply-To preserve the RFC thread.
   Incoming Outlook Thread-Topic/Thread-Index are snapshotted per request; replies
   preserve the topic and append a validated response block to the parent index.
   Recipient-side conversation grouping still requires an actual client check.
   Primary-body links and quoted history are supplied as a separate, untrusted
   reference section (maximum 16 KiB UTF-8), never as invocation authority. HTML
   HTTP(S) link destinations are retained even when the plain-text alternative
   contains only a label. Attached-message branches are not flattened into it.
5. If clarification is needed, the agent calls `email_ask_owner`. The durable
   waiting state suppresses final text and attachment emails. The question is
   sent privately, never to the group. The question tool explicitly stops the
   bound email agent turn; it does not rely on a silent model response. Repeated
   calls while a question is pending retain its original token and notice.
6. After the owner's correlated answer, the poller queues a continuation to the
   original email session. Each answered-question generation has a distinct
   native input identity, while outgoing RFC reply headers still target the
   original email. An explicit request authorizes its requested reply;
   there is no mandatory approval prompt for every successful send.
7. Completion is reported only after SMTP acceptance. This is not proof of
   delivery to an inbox or that anyone read it.

Owner aliases are mapped to one routing identity, while unrelated RFC threads
remain separate. Other recipients can receive answers without gaining command
authority. The first sorted configured owner address currently supplies the
canonical owner route; changing the set so its first address changes may start
new sessions. Keep the owner alias set stable during pending work.

Only current-author explicit invocation triggers work. Recognized quoted,
forwarded and signature content is not a new instruction. HTML blockquotes and
common quoted containers are excluded before invocation detection. This is
not a promise to recognize every localized/custom mail client's quote format.
Malformed or ambiguous recipient lists are not silently repaired, truncated or
sent only to the owner. Unexpected Reply-To redirection outside the visible
participants fails the request privately so the owner can send corrected mail.
Bcc recipients are never discovered or disclosed.

Limits inherited or imposed by this build include 100 unique visible outbound
recipients, bounded address headers, and the upstream attachment limits. SMTP
mailboxes must use ASCII local parts/domains; Unicode display names are fine.
Quoted content and attachments remain untrusted reference data. The integration
is not an OS/tool sandbox: the owner's full-tool Hermes runtime retains its
existing privileges. Do not grant other email senders owner authority merely
to let them receive a summary.

## Private backchannel

Configure exactly one explicit private target and its authenticated owner ID:

- `telegram:<chat-id>[:<topic-id>]`
- `discord:<channel-id>[:<thread-id>]`
- `fluxer:<channel-id>[:<thread-id>]`
- `signal:<chat-id>`

Use actual route and user IDs, not display names or mutable channel names.
Choose a genuinely private destination. The configured outgoing target must
match the incoming command's normalized source identity; verify this for the
selected platform during acceptance, especially phone-number/UUID aliases.

Notifications invoke the supported `hermes send --json --to ... --file -` CLI,
with the owning profile pinned and text passed on stdin. This is a bounded
no-LLM subprocess, not another gateway/server. The plugin does not use the
unavailable registry `send_message` tool or import a private sending API.

Questions include a deterministic answer command:

```text
/email-answer your answer
/email-answer #7 your answer
/email-status
/email-cancel <request-id>
```

With one pending question, no IDs are needed. With several, use the short
number shown beside the question (for example `#7`); ambiguous answers are not
consumed. Numbers are persisted per question, survive restart, and are never
reassigned to later questions. The owner/private-destination and expiry checks
still apply. The old two-ID answer format remains a compatibility fallback,
but is no longer advertised in question messages.

The current release uses these explicit commands rather than promising to
interpret every unstructured chat reply. IDs are generated per request/question;
stale, duplicate, wrong-user, wrong-destination and expired answers are rejected.
A generic unrelated “yes” is not an approval. Questions expire after one day;
the request becomes expired and no email is sent. A subsequent request can
continue the conversation.

Typical success generates two private notices: accepted and SMTP accepted.
Additional notices cover questions and failures. Notification outages retain an
outbox entry and retry later; they do not turn an uncertain email send into a
new send. A lost chat acknowledgment can produce a duplicate status notice,
not a duplicate email.

## Persistence and recovery

Runtime state is profile-local:

- `email_collaboration/state.sqlite3`: requests, immutable recipient snapshots,
  question generations/answers, delivery outcomes and notification outbox.
- `email_collaboration/gateway.lock`: one recovery/polling owner per profile.
- `email_pp/threads.json`: upstream RFC-thread to Hermes-thread routing.

Directories/database are owner-only. SQLite transactions serialize claims and
answers. Only one live worker may recover the profile. On restart interrupted
working requests become ready; waiting questions remain pending. A request
that was sending becomes `delivery_uncertain`, not automatically resent.
Preserve both the database and thread mapping when backing up. These contain
private task content; do not commit them. Backups do not prove a tested restore.
Retention/pruning is not yet automated; operators must manage this private
state and must not delete terminal deduplication records while retained unread
messages can be polled again.

SMTP does not offer unconditional exactly-once semantics. A connection lost
after DATA may mean the server accepted the email. Such outcomes and partial
recipient refusals require inspection, not a blind group resend. Messages retain
the generated outbound Message-ID for investigation.

## Activation and rollback

After saving settings, inspect the effective disabled state and plugin health:

```console
hermes plugins doctor email-pp
hermes config get platforms.email_pp.enabled
```

A plugin enabled for CLI discovery is separate from `platforms.email_pp.enabled`.
The running gateway can still be the pre-install process. Coordinate a restart
window before enabling the transport; do not interrupt active chats just to make
a status screen green. Use the supported config command when activation is
approved, verify mailbox/folder authentication, then restart the intended gateway
and perform the real acceptance checks below. No global provider/model setting
needs to change.

Rollback: disable `platforms.email_pp.enabled`, stop/restart only the authorized
gateway at the agreed window, and disable the plugin if desired. Retain the
private database and mailbox contents for recovery. Re-enabling after rollback
must reconcile uncertain sends before any manual retry.

## Verification

Use a project-local test environment with the intended installed Hermes source
and dependencies available, never install test packages into the production
Hermes environment. The tests set a disposable HERMES_HOME before imports and
block external sockets; no real email or chat credentials are used.

```console
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests -q
.venv/bin/ruff check hermes_email_pp tests
.venv/bin/ruff format --check hermes_email_pp tests
.venv/bin/mypy --explicit-package-bases hermes_email_pp --ignore-missing-imports --follow-imports=silent
hermes plugins validate /path/to/checkout --json
.venv/bin/python -m build --wheel
```

The host delivery integration exercises the real BasePlatformAdapter background
turn/final-response/completion path and a real loopback SMTP dialogue. The model
boundary and external TLS service are fixtures; it is not a Namecheap delivery
or actual-model acceptance claim. Policy/store/setup tests cover forged
headers, group recipients, quoting, concurrent transactions, restart recovery,
private answer binding, SMTP uncertainty and secret-safe setup.

Before production use, verify with consenting external test recipients:

- Visible alias From and Reply-To, authentication headers and returning replies.
- Original To/Cc group and subject/RFC threading in actual mail clients.
- Selected private chat acknowledgment, question, authorized answer and resume.
- Unauthorized sender rejection and passive CC silence.
- Two concurrent threads, owner alias changes, restarts and send failures.
- No unrelated mailbox content or private backchannel text sent to the group.

Packaging/doctor/tests do not replace these live checks.
