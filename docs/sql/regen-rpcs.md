# `claim_stale_recipes` and `regen_failed` — required semantics

**Status:** reference, not a migration. Nothing in this repo applies it. These two
functions live in Cayenne migration `014_regen_rpcs.sql`; this file is what that
migration has to implement, so it can be handed to whoever writes it.

**Why this file exists.** Spec §5.3 and §5.6
(`docs/superpowers/specs/2026-09-07-recipe-edit-philosophy-design.md`) define
claiming and failure as concrete SQL. The plan replaced that SQL with two RPC
names and recorded only their signatures, so nothing in this branch said that
`claim_stale_recipes` must set `claimed_at`, apply the 20-second debounce,
honour the 5-minute lease, or exclude rows that have already failed three times
on the current revision — all of which are load-bearing. It is kept here rather
than as an ARCHITECTURE.md appendix because it is reference SQL for a migration
in a *different* repository: it needs to travel to the Cayenne author intact,
and ARCHITECTURE.md §13 describes this service at a higher altitude.

The only Python that calls these is
`recipeparser/adapters/regen_worker.py` (`RegenWorker.run_once` and
`RegenWorker._process`).

## Calling convention

supabase-py sends RPC arguments as a named JSON object, so the **parameter names
are part of the contract** and must be exactly `p_limit`, `p_id` and `p_msg`:

```python
supabase.rpc("claim_stale_recipes", {"p_limit": self._batch}).execute()
supabase.rpc("regen_failed", {"p_id": rid, "p_msg": str(exc)[:2000]}).execute()
```

Both are called with the **service-role** key from inside the API process.
Grant `execute` to `service_role` only; revoke it from `anon` and
`authenticated`. Neither is ever called by the client.

---

## `claim_stale_recipes(p_limit integer)`

Atomically selects up to `p_limit` stale recipes, marks them claimed, and
returns them. It must do **all** of the following (spec §5.3):

| Rule | Predicate | Why it is load-bearing |
|---|---|---|
| Stale only | `derived_rev < body_rev` | Staleness *is* the queue (D5). There is no separate flag. |
| Quiet window | `updated_at < now() - interval '20 seconds'` | The debounce: a burst of keystroke-level saves must produce one regen, not one per save. Each regen costs a REFINE call and an embedding call. |
| Lease | `claimed_at is null or claimed_at < now() - interval '5 minutes'` | A row claimed by a worker that then died must become claimable again, and only then. |
| Attempt cap | `derived_attempts_rev <> body_rev or derived_attempts < 3` | Stops a permanently-failing row from burning two Gemini calls every five minutes forever. The `<>` half is the reset: a *new* `body_rev` gets a fresh three attempts. |
| Order | `order by updated_at` | Oldest edit first; matches the partial index in spec §7.1. |
| Claim | set `claimed_at = now()` on exactly the rows returned | Without this the same rows are re-claimed on the next poll and processed twice concurrently. |

The claim and the select must be one statement — a `select` followed by a
separate `update` races two workers (or two API instances) against each other.

Returned columns are the ones `build_extraction` and `_process` read:
`id`, `user_id`, `title`, `ingredient_lines`, `direction_steps`, `body_rev`.
`_process` reads `row["id"]`, `row["user_id"]` and `row["body_rev"]` directly,
so all three must always be present and non-null.

### Reference SQL

```sql
create or replace function public.claim_stale_recipes(p_limit integer)
returns table (
  id               uuid,
  user_id          uuid,
  title            text,
  ingredient_lines jsonb,
  direction_steps  jsonb,
  body_rev         integer
)
language sql
security definer
set search_path = public
as $$
  with candidates as (
    select r.id
      from recipes r
     where r.derived_rev < r.body_rev
       and r.updated_at < now() - interval '20 seconds'
       and (r.claimed_at is null or r.claimed_at < now() - interval '5 minutes')
       and (r.derived_attempts_rev <> r.body_rev or r.derived_attempts < 3)
     order by r.updated_at
     limit p_limit
     for update skip locked
  )
  update recipes r
     set claimed_at = now()
    from candidates c
   where r.id = c.id
  returning r.id, r.user_id, r.title,
            r.ingredient_lines, r.direction_steps, r.body_rev;
$$;

revoke all on function public.claim_stale_recipes(integer) from public, anon, authenticated;
grant execute on function public.claim_stale_recipes(integer) to service_role;
```

### Caveat for the migration author: `moddatetime`

Spec §7.1 puts a `moddatetime` trigger on `recipes.updated_at` for **every**
update. The claim above is an update, so claiming a row bumps its `updated_at`.
Consequences to be aware of before applying:

- `updated_at` stops meaning "last user edit" and starts meaning "last change of
  any kind". The 20-second quiet window is then measured from the claim as well
  as from the edit. This does not break claiming (the row is claimed and
  processed immediately), and the 5-minute lease still governs re-claim.
- `regen_failed` also bumps it, which gives a failed row an extra 20 seconds
  before it can be re-claimed — a small, harmless backoff.

If the intent is for the debounce to track user edits strictly, condition the
trigger so that updates touching only `claimed_at` / `derived_*` do not bump
`updated_at`. That is a decision for the migration, not a requirement of this
service; both behaviours satisfy the worker.

---

## `regen_failed(p_id uuid, p_msg text)`

Records one failed regen attempt. Called from `RegenWorker._process`'s exception
handler for every failure — Gemini, embedding, or the write-back itself.

It must (spec §5.6):

- set `derived_error = p_msg`;
- increment `derived_attempts` **only when the counter already refers to the
  current revision** (`derived_attempts_rev = body_rev`), and otherwise reset it
  to `1` — this is what gives a newly edited recipe a fresh set of attempts;
- set `derived_attempts_rev = body_rev`, stamping the counter with the revision
  it counts against;
- clear `claimed_at`, so the row is immediately re-claimable until the cap is hit.

It must **not** touch `derived_rev` (the row stays stale) and must **not** touch
any derived column.

After three failures on the same revision the row stops matching
`claim_stale_recipes`'s attempt-cap predicate and drops out of the queue until
`body_rev` changes.

### Reference SQL

```sql
create or replace function public.regen_failed(p_id uuid, p_msg text)
returns void
language sql
security definer
set search_path = public
as $$
  update recipes set
    derived_error        = p_msg,
    derived_attempts     = case when derived_attempts_rev = body_rev
                                then derived_attempts + 1 else 1 end,
    derived_attempts_rev = body_rev,
    claimed_at           = null
  where id = p_id;
$$;

revoke all on function public.regen_failed(uuid, text) from public, anon, authenticated;
grant execute on function public.regen_failed(uuid, text) to service_role;
```

`p_msg` is already truncated to 2000 characters by the caller.

The worker wraps this call in its own `try/except`: if `regen_failed` is missing
or the database is unreachable, the failure is logged and the row keeps its
`claimed_at` until the lease expires. That is a degraded mode, not a substitute
for the function existing — without it no row ever accumulates attempts and the
cap does not apply.
