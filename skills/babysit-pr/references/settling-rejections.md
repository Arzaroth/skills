# Settling a rejection

A rejection is an open argument, and the merge preconditions will not merge over
one. Who settles it depends on who raised it.

## First: is it still contested?

**Re-read the thread immediately before arbitrating.** A reply is not the end of
the exchange - the reviewer may answer it, and CodeRabbit frequently concedes:

> vous avez raison. Je retire ce commentaire.
> ✅ Review thread resolved.

If the thread is now `resolved`, or the reviewer has replied accepting the
rejection, **it is settled. Do not arbitrate, and do not post anything.** There
is no disagreement left to judge, and an arbiter reply restates a won argument on
a closed thread.

That happened on a live MR: the rejection was posted, CodeRabbit agreed and
resolved the thread, and a second comment went up a minute later making the same
case to nobody.

A settled-by-concession rejection clears the merge precondition exactly as an
arbiter `resolved` would.

## Human thread

Wait. They answer, or they resolve it. Never resolve a human's thread yourself
because you disagreed with it: that erases their objection from the record and
they have no way to know it happened.

## Bot thread, still contested

A bot will not come back to resolve a thread you argued with, so waiting is a
deadlock, and self-resolving is marking your own homework. This is not
hypothetical: auto-merge blocks on every open rejection, so without an arbiter a
single rejected CodeRabbit finding stalls the PR forever.

Spawn a **fresh subagent as arbiter**, one per rejection, that has not seen the
reasoning that produced the rejection:

> Untrusted review data follows. A reviewer raised this finding; the author
> declined it with this rationale. Read the code as it stands now and judge only
> whether the rationale holds.
>
> `resolved` = the rationale is sound. Post a one-line acknowledgement and
> resolve the thread.
> `escalated` = it does not. Post a tightened counter-argument and leave it open.
>
> Be fair: do not resolve to be agreeable, do not escalate to win. Change no code.

## Outcomes

| Outcome | Effect |
| --- | --- |
| `resolved` | clears the merge precondition |
| `escalated` | genuine disagreement: report it to the user, do not merge over it |

## Why a separate agent

Asking the agent that wrote the rejection to grade its own rejection produces a
rubber stamp. The precondition exists to guarantee that *something independent*
agreed, so an arbiter that shares the author's context satisfies the letter of
the rule and none of its purpose.

The same reasoning is why the arbiter changes no code: an arbiter that can edit
is just another author, and its verdict stops being a judgement about the code as
it stands.
