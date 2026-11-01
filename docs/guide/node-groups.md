# Node groups (owner federation)

A **node group** joins the Atlas nodes that one owner runs (for example a home server, a VPS and a laptop) so
that they recognise each other. This release builds the group itself: membership certificates, revocation,
enrolment and recovery. Profile sync and cross-node sign-in come in later releases; nothing about your accounts
is shared by joining a group.

Membership is private. It is never written to the public signed feed or `/api/v2/changes`, `/sync/info` does
not mention it, and every group endpoint answers the same `404 Not Found` to anyone who cannot prove membership.

## Create a group

**Administration → Node group → Create a group on this node**, or:

```
atlas group create --name "My nodes" --recovery-out /safe/place     # asks for the passphrases
```

- The node that creates the group keeps the **group root key**. It is encrypted twice: with a root passphrase
  (scrypt N=2^17, r=8, p=1 and AES-256-GCM) and then with `ATLAS_SECRET_KEY`. Owner groups need `ATLAS_SECRET_KEY`.
- Passphrases need at least 14 characters (20 if they use only one kind of character), must not contain the
  node or group name, and are checked against a list of common passwords and keyboard patterns.
- You get a **recovery file** (1-of-1 by default; `--recovery 2-of-3` makes three files, any two of which can
  replace the root, none holding the root key). Download it and confirm its passphrase once more: root operations
  stay locked until you have.

**Honest limits.** A stolen database **without** `ATLAS_SECRET_KEY` gives nothing. A stolen database (or
backup) **plus** `ATLAS_SECRET_KEY` does not reveal the root key directly, but it allows **unthrottled offline
guessing** of the root passphrase at scrypt cost (N=2^17, r=8: about 128 MiB and 0.3-0.5 s per guess per core). A
random 14-character passphrase resists that; a human-chosen phrase may not. Owners who keep backups together with
the secret key should use the offline-root mode. This release does not offer the offline-root mode yet: until it
does, keep backups apart from `ATLAS_SECRET_KEY` and use a random root passphrase.

Someone who obtains a recovery file can guess its passphrase offline in the same way. A 1-of-1 file holds both the
recovery key and the root key, so a stolen and cracked file allows a full takeover; a succession issued with it is
pending for 24 hours, shown on every member, and cancellable with your own copy (the two then conflict, which means
creating a new group). Store recovery files offline, use a different passphrase for them, or use `--recovery
2-of-3`. Downloading the recovery file is audited (`group_root_exported`).

Every root operation in the admin UI (issuing a certificate, revoking, renewing) asks for your current account
password and the root passphrase, and is limited to 5 attempts per hour per administrator.

## Add a node

1. On a node that holds the root (or was delegated `group.enrol`): **Add a node → Create invite**, or
   `atlas group invite`. You get the address of this node and a one-time code `AG1-…`, valid for 10 minutes.
2. On the new node: **Node group → Join an existing group**, or `atlas group join --url URL --code CODE`.
3. Both screens show a **12-digit number** and the other node's fingerprint. Compare them over a channel you
   trust. Tick "The numbers match" on the inviting node (with the root passphrase) and then on the new node.

If the numbers differ, cancel: someone who saw the code tried to join in place of your node. Five wrong
attempts burn an invite, at most three invites are open at once, and enrolment endpoints are rate limited per IP.

The inviting node tries to reach the new node at the address it claims. When it cannot (a laptop behind NAT),
the member shows **address not verified**; that is informational.

## Members and permissions

**Node group** lists every member with its fingerprint first, its address, its permissions (`profile.sync`,
`group.relay`, `identity.assert` by default), the certificate serial and expiry (365 days; delegates 30 days),
its status and when it was last seen. Renew certificates from the same page or with `atlas group renew --all`.

**Revoke** a member as `removed`, or as `compromised` with the time from which it is suspect. Both ban the node:
it can only come back by a new, root-issued readmission. Revoking a node that enrolled others also revokes
everything it enrolled. `atlas group revoke --node NODE_ID [--reason key_compromise --suspect-since TIME]`.

Revocations are append-only on every member: a later list cannot undo one, and forks or rewinds raise an alarm
for the administrators.

## Freshness

A member that could be withholding revocations is caught by the others: each member signs a short **view** of
the revocation lists it holds, and delegated or privileged actions wait until a view from a third member is
newer than `ATLAS_GROUP_VIEW_MAX_AGE_HOURS` (24 h by default). In a group of two the rule cannot apply and is
waived. Members pull each other's state every `ATLAS_GROUP_SYNC_INTERVAL` seconds (60) and on **Sync with
members now** (`atlas group sync`). Member-to-member calls are encrypted to the receiving node and signed, so
proxies and logs see only `POST /api/v2/group/call`.

After a **restore from backup** a node is `resyncing`: it pulls every member's state before any root or
delegated action. `atlas group resync --skip` (or the button, with your password) skips that and is audited.

## Leaving, and losing the root

- `atlas group leave` revokes this node's own certificate; the others learn it on their next pull.
- A node that holds the root cannot leave until a recovery file that holds the root has been exported and
  verified (`atlas group root export-recovery --out FILE`, then `verify-recovery`), so the root is never destroyed
  by accident.
- **The root node is lost, the root key is not compromised:** on another member run
  `atlas group root import --recovery-file FILE --old-root decommissioned` (or `running` if the old root node still
  runs). It needs a 1-of-1 file (the only kind that holds the root key) and sets a new root passphrase. The node
  signs a `root_moved` notice; a still-running old root node disables its own root when it sees it. The new holder
  is `resyncing` until it has pulled every member (`atlas group sync`, or `atlas group resync --skip` when the old
  node is gone for good).
- **The root key is stolen:** `atlas group root succession --recovery-file FILE [--recovery-file FILE2]
  [--exclude-node NODE_ID] --recovery-out DIR` signs a **root succession** with a recovery quorum and a new root
  key held on this node. It carries every current member except the excluded ones (revoke the breached node too).
  It activates after 24 hours; in that window any recovery quorum can cancel it with
  `atlas group root succession-cancel --recovery-file FILE`, and objects signed by the old root are held. A
  cancellation signed only by the root is never honoured. With a 1-of-1 file a new recovery file holding the new
  root key is written to `DIR`.
- Not in this release: offline root signing (`root sign`/`import`), `rekey-box` and `member-update`.

## What remains observable

- A network observer or TLS terminator learns that two addresses exchange group traffic, and how much.
- Anyone who may scrape `/metrics` learns that this node is in a group and the member node ids (the `group`
  collector appears only while the node is in a group). Restrict `/metrics` with the `metrics` token scope if
  that matters.

## Downgrading

Migration `0028` adds the group tables. Downgrading below it is refused while this node is in a group or holds
the sealed root; leave the group first.
