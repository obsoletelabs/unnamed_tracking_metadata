# Linking to a main Atlas

A **main Atlas** is a public authority node such as a community catalogue. If you run your own Atlas at home
(a *local node*) and have an account on a main, you can **link** the two. Your node can then propose its edits to
the main under your account. The main's reviewers decide on every proposal under the main's own rules, exactly as
for edits typed on its website.

Linking never copies your node's data to the main by itself. Only the edits you choose to propose are sent, one
at a time. The main never gets your node's own signing key, its users or its private data.

## On your node: Admin → Main Atlas

1. Type the main's address (for example `https://atlas.example.org`) and choose **Check**. The node reads the
   main's public description and shows its **fingerprint**. Compare it with the fingerprint the main shows under
   *Admin → Signing keys*. Your node keeps this key and refuses another one later.
2. Choose **Link to …**. Your node makes a **new key for this main only**, so two mains cannot tell that they talk
   to the same node. The page then shows three things:
   - a **QR code** of the approval link,
   - the **code** (like `WDJB-MJHT`), which the main asks you to type,
   - a **confirmation code**, which you type on the main as well. It is shown only on your node and is never part
     of the link, so a link someone sent you cannot be approved without your node.
3. On the main, approve the link (below), then choose **I approved it** on your node. The node also checks by
   itself every 30 seconds.

An **authority cannot link upward**: it is a main itself, so it has no *Main Atlas* page.

## On the main: approving the link

Open the QR code on a phone that is signed in to the main, or go to `/link` on the main and type the code. The
main then shows:

- the name your node chose, its **fingerprint** and the fingerprint of its link key,
- the address the request came from and when it was started.

Check that the fingerprint matches the one on your node. Then type the **confirmation code** from your node's
screen and your **password**, plus the code from your authenticator if your account uses two-factor sign-in.
Approval is never done by just opening a link.

The main also enforces these rules:

- Accounts younger than one day cannot link nodes yet.
- A session opened through another node of an owner group cannot approve links unless the main allows it
  (`link_federated_approval`).
- After five wrong confirmation codes the request is refused, and you start again on your node.
- Each account can approve at most 10 links a day. A main accepts at most `link_main_new_daily` new links a day
  (50 by default).

Optionally, choose **Show my display name**. If you do, reviewers see "dave (via node NAS Atlas)". If you do not,
they see "a linked node (5f0c…)", a short pseudonym. The main gives each linked node a different pseudonymous
subject for you, never your account id or email address. You can change this later on *Linked local nodes*
(`/me/links`).

## Probation

**Every new link starts on probation, and every relink starts it again.** While a link is on probation, everything
it sends waits for a reviewer, even if your account could publish directly on the website. The link earns trust from
its own approved work. When reviewers have approved enough of it (the main's auto-approve reputation), the link
leaves probation. After that, a proposal through the link is applied at once only if your account's
**reputation** allows it. Reviewer or administrator rights never auto-apply anything sent through a link. A
reviewer also cannot approve proposals from their own linked node.

A main can set `link_review_default = always`. All link proposals are then reviewed, whatever the trust.

## Proposing edits

Once linked, an administrator of your node sees **Where this edit goes** on the edit form: tick *Also propose it to
…*. The edit is applied on your node as usual and queued for the main.

With `link_push = auto`, every proposal approved on your node is also queued. The default is `manual`.

The node signs each queued item with its link key and sends the queue every 30 seconds:

- It sends items in order, at most 20 per request.
- When the main asks it to wait (`429`, or `Retry-After`), it waits. It stops completely if the main refuses the
  link.
- *Send and check now* on the *Main Atlas* page does it at once.

The queue table shows each item's state:

| State | Meaning |
| --- | --- |
| `queued` | Waiting to be sent. |
| `pending` | Waiting for review on the main. |
| `approved`, `rejected`, `reverted`, `withdrawn` | The main's decision. |
| `failed` | The main refused it for good; the reason is shown. |
| `link_revoked` | The link ended before it was sent. |
| `unsent` | You unlinked before it was sent. |

A new record approved on the main is known there under the main's id. Your node remembers which local record that
is.

If your node is in an owner group, the other members see these states too ([node groups](node-groups.md)), but
never the edits themselves.

Images can be proposed only as public `https` addresses. Uploading images to a main is not available yet.

### Limits on the main

- **Per link:** 10 a minute, 200 a day and 50 waiting.
- **Across all links of one node key:** 400 a day.
- **Per account across all its links:** 400 a day and 100 waiting.
- **For the whole main:** `link_main_daily` (5 000 by default).

A refusal for a limit says when to try again. The node keeps the item, and everything after it, queued until then.

## Unlinking, key rotation and a stolen key

**Unlink** on your node takes effect at once: nothing more is sent, and queued items become `unsent`. The node then
tells the main and shows *unlinked here, main not told yet* until the main has confirmed. The worker keeps trying.
You, and the main's administrators, can also unlink from the main's side.

Proposals that were already waiting stay in the main's review queue. They are marked "linked node removed".

**Rotate link key** makes a new key for this link. The new key is appended to the link's key history, so the main
can check it from the first key. The node signs everything still queued again with the new key. Old tokens stop
working at once.

If the main's answer to a rotation is lost (a timeout, a dropped connection, an answer the node cannot check), the
node keeps the new key and sends nothing else until the rotation is finished. Pressing **Rotate link key** again, or
the next worker round, repeats exactly the same rotation. The main recognises the repeat for up to an hour and
answers with tokens for the new key; the link is not frozen. After that hour the link has to be linked again.

The node only treats the link as ended when the main says so: the link was revoked, frozen or banned, or the account
is gone. An expired token is refreshed. A refused proof, for example because the node's clock is more than five
minutes off, leaves the link and its queue as they are, and the node tries again later. Fix the clock if the error
keeps coming back.

If you think the node's key was **stolen**, unlink with *This node's key may have been stolen*. The main then marks
the link's waiting proposals "node key may be stolen". A reviewer must tick *I checked this change* before approving
one of them.

A main **freezes** a link by itself when someone else rotates its key in a way that does not extend the key history
it knows. A frozen link refuses everything. To continue, choose *Link again* on your node with *Relink the same
account*. The node makes a new key, you approve again on the main, and the link starts on probation again.

A main's administrator can **ban** a node key under *Admin → Node links*. No key of that link's history can link
again, under any account.

On the main, *everything from this link* in the review queue lists what one link sent: the proposals still waiting,
and under **Applied from this link** the ones already applied. A reviewer can revert one of them, or all of them at
once with **Revert everything this link applied**. That reverts newest first. A change with a newer change from
elsewhere on top of it is left applied and stays listed, so a reviewer can undo it by hand. Entries already published
in the main's public feed stay there.

## Settings

| Setting | Where | Default | Effect |
| --- | --- | --- | --- |
| `link_enabled` | main | on | Accept node links. Only on an authority. |
| `link_review_default` | main | `standard` | `always` reviews every link proposal. |
| `link_main_daily` | main | 5000 | Link proposals accepted per day from all nodes together. |
| `link_main_new_daily` | main | 50 | New links per day (0 accepts none). |
| `link_federated_approval` | main | off | Let a session opened through another node approve links. |
| `link_push` | node | `manual` | `auto` also queues proposals approved on the node. |

Following a main's catalogue is separate from linking. It uses a sync token. An administrator of the main grants
the **feed allowed** right per account under *Admin → Users*; it is never part of a link. The account can then make
its own sync tokens, which expire after a year.

## Security notes

These are for administrators and auditors.

- **Device flow.** Linking uses the OAuth device flow (RFC 8628). The node first proves it holds its key by signing
  a one-time challenge from the main.
- **Bound tokens.** Every later request carries a DPoP proof (RFC 9449) by the link's current key. A token is
  accepted only with that key, at the link's current key epoch. A superseded key never works again.
- **What the main stores.** It keeps codes only as a hash or an HMAC. It keeps tokens as hashes. It records each
  proof id once, so a replayed request is refused.
- **What the node stores.** It keeps its link key, its tokens and the open confirmation code encrypted with
  `ATLAS_SECRET_KEY`. `atlas secrets rewrap` and `atlas backup verify --secret-check` cover them.
- **Pseudonymous subjects.** The main derives them from a key of its own, kept the same way. Losing that key
  changes every subject.

The wire protocol and its checks are specified in the contract (CONTRACT-C14 Part C).
