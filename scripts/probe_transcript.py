#!/usr/bin/env python3
"""Phase 0 probe of the Conductor v0 API.

The OpenAPI spec types a transcript message's ``content`` as ``{}`` — completely
untyped — and ``type`` as a bare string. The renderer cannot be written against
that, and several load-bearing assumptions in the poller design (is ``after=``
exclusive? does re-POSTing the same ``messageId`` dedupe?) are unverified. This
script answers both questions against the live API and writes fixtures the rest
of the project is built on.

Two modes, split so the default cannot touch anything:

    # READ-ONLY. Dumps transcripts, reports content shapes, samples the SQL view.
    python scripts/probe_transcript.py dump --session SID [--session SID ...]
    python scripts/probe_transcript.py dump --auto          # discover sessions itself

    # WRITES. Sends throwaway prompts to ONE session you nominate.
    python scripts/probe_transcript.py assume --session SCRATCH_SESSION_ID

``assume`` posts real prompts and costs real tokens — point it at a scratch
session, never at work in progress.

Output lands in ./probe-out/ (gitignored — it contains real transcript text).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - operator-facing
    sys.exit("httpx is required:  pip install httpx")

API_URL = os.environ.get("CONDUCTOR_API_URL", "https://api.conductor.build/v0")
API_KEY = os.environ.get("CONDUCTOR_API_KEY", "")

# The API sits behind a proxy that 403s some default client signatures (the docs
# call out Python's urllib). Always send an explicit UA.
USER_AGENT = "conductor-tg-bot-probe/0.1 (+https://github.com/Reclaimly)"

OUT = Path(__file__).resolve().parent.parent / "probe-out"

# ── plumbing ────────────────────────────────────────────────────────────────


class Probe:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.c = client
        self.calls: list[dict[str, Any]] = []

    async def req(
        self, method: str, path: str, *, raise_for_status: bool = True, **kw: Any
    ) -> tuple[int, Any]:
        """Return (status_code, parsed_body). Never raises on 4xx unless asked."""
        t0 = time.monotonic()
        r = await self.c.request(method, path, **kw)
        dt = int((time.monotonic() - t0) * 1000)
        try:
            body = r.json()
        except Exception:
            body = {"_raw": r.text[:2000]}
        self.calls.append(
            {"method": method, "path": path, "status": r.status_code, "ms": dt}
        )
        if raise_for_status and r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {body}")
        return r.status_code, body

    async def get(self, path: str, **kw: Any) -> Any:
        _, body = await self.req("GET", path, **kw)
        return body

    async def messages(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        after: str | None = None,
        raise_for_status: bool = True,
    ) -> tuple[int, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if after is not None:
            params["after"] = after
        return await self.req(
            "GET",
            f"/sessions/{session_id}/messages",
            params=params,
            raise_for_status=raise_for_status,
        )

    async def all_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Page the full transcript via offset."""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            _, body = await self.messages(session_id, limit=100, offset=offset)
            page = body.get("data", [])
            out.extend(page)
            if not body.get("hasMore") or not page:
                break
            offset += len(page)
            if offset > 20_000:  # runaway guard
                print("  ! stopped paging at 20k messages")
                break
        return out


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def write(name: str, text: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    p.write_text(text)
    return p


# ── A + B: dump and shape report ────────────────────────────────────────────


def describe(value: Any, depth: int = 0, max_depth: int = 3) -> Any:
    """Structural description of a JSON value: shapes, not data."""
    if depth >= max_depth:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        return {k: describe(v, depth + 1, max_depth) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return "list<empty>"
        inner = {
            json.dumps(describe(v, depth + 1, max_depth), sort_keys=True) for v in value
        }
        if len(inner) == 1:
            return [json.loads(next(iter(inner)))]
        return [json.loads(s) for s in sorted(inner)][:3]
    if isinstance(value, str):
        return f"str(len={len(value)})"
    return type(value).__name__


def collect_string_leaves(value: Any, acc: list[int]) -> None:
    if isinstance(value, str):
        acc.append(len(value))
    elif isinstance(value, dict):
        for v in value.values():
            collect_string_leaves(v, acc)
    elif isinstance(value, list):
        for v in value:
            collect_string_leaves(v, acc)


def pct(vals: list[int], p: float) -> int:
    if not vals:
        return 0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def shape_report(by_session: dict[str, list[dict[str, Any]]]) -> str:
    lines: list[str] = [
        "# Conductor transcript shape report",
        "",
        f"Generated {utcnow()}",
        "",
    ]

    all_msgs = [m for msgs in by_session.values() for m in msgs]
    lines += [
        f"Sessions probed: {len(by_session)}",
        f"Total messages:  {len(all_msgs)}",
        "",
        "## `type` histogram",
        "",
    ]
    hist = Counter(m.get("type") for m in all_msgs)
    for t, n in hist.most_common():
        lines.append(f"- `{t}` — {n}")

    # sessionIndex integrity, per session (assumption test 2)
    lines += ["", "## `sessionIndex` integrity", ""]
    for sid, msgs in by_session.items():
        idx = [m.get("sessionIndex") for m in msgs]
        numeric = [i for i in idx if isinstance(i, (int, float))]
        unique = len(set(numeric)) == len(numeric)
        monotonic = all(a < b for a, b in zip(numeric, numeric[1:], strict=False))
        gapless = bool(numeric) and (
            max(numeric) - min(numeric) == len(numeric) - 1  # type: ignore[operator]
        )
        lo, hi = min(numeric, default=None), max(numeric, default=None)
        lines.append(
            f"- `{sid}` n={len(numeric)} unique={unique} monotonic={monotonic} "
            f"gapless={gapless} range={lo}..{hi}"
        )

    # per-type content shape
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in all_msgs:
        by_type[str(m.get("type"))].append(m)

    lines += ["", "## `content` shape by type", ""]
    for t, msgs in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        lines += [f"### `{t}`  ({len(msgs)} messages)", ""]

        shapes = Counter(
            json.dumps(describe(m.get("content")), sort_keys=True) for m in msgs
        )
        lines.append(f"Distinct content shapes: {len(shapes)}")
        lines.append("")
        for shape_json, n in shapes.most_common(5):
            lines += [
                f"<details><summary>shape ×{n}</summary>",
                "",
                "```json",
                json.dumps(json.loads(shape_json), indent=2),
                "```",
                "</details>",
                "",
            ]

        leaves: list[int] = []
        for m in msgs:
            collect_string_leaves(m.get("content"), leaves)
        if leaves:
            lines.append(
                f"String leaves: n={len(leaves)} min={min(leaves)} "
                f"p50={pct(leaves, 0.5)} p95={pct(leaves, 0.95)} max={max(leaves)}"
            )
            lines.append("")

        lines += ["Samples:", "", "```json"]
        for m in msgs[:3]:
            lines.append(json.dumps(m, indent=2)[:4000])
        lines += ["```", ""]

    return "\n".join(lines)


async def cmd_dump(p: Probe, session_ids: list[str], auto: bool) -> None:
    if auto:
        print("Discovering sessions via the SQL view…")
        _, body = await p.req(
            "POST",
            "/sql",
            json={
                "query": (
                    "SELECT session_id, session_title, workspace_name, agent_type, "
                    "model, workspace_state, transcript_updated_at "
                    "FROM session_transcripts_view "
                    "ORDER BY transcript_updated_at DESC LIMIT 25"
                )
            },
        )
        rows = body.get("rows", [])
        write("sessions_discovered.json", json.dumps(rows, indent=2))
        for r in rows[:20]:
            print(
                f"  {r.get('session_id')}  {r.get('workspace_name')!r:30} "
                f"{r.get('agent_type')}/{r.get('model')}  {r.get('workspace_state')}"
            )
        if not session_ids:
            session_ids = [r["session_id"] for r in rows[:4] if r.get("session_id")]
            print(f"\nAuto-selected {len(session_ids)} most recent sessions.")

    if not session_ids:
        sys.exit("No sessions to dump. Pass --session SID or use --auto.")

    by_session: dict[str, list[dict[str, Any]]] = {}
    jsonl = OUT / "transcripts.jsonl"
    OUT.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w") as fh:
        for sid in session_ids:
            print(f"Dumping {sid} …", end=" ", flush=True)
            try:
                msgs = await p.all_messages(sid)
            except RuntimeError as e:
                print(f"FAILED: {e}")
                continue
            by_session[sid] = msgs
            for m in msgs:
                fh.write(json.dumps(m) + "\n")
            print(f"{len(msgs)} messages")

    # E: what does the SQL view's transcript column actually look like?
    print("Sampling session_transcripts_view …")
    _, body = await p.req(
        "POST",
        "/sql",
        json={"query": "SELECT * FROM session_transcripts_view LIMIT 3"},
    )
    write("sql_view_sample.json", json.dumps(body, indent=2))
    rows = body.get("rows", [])
    if rows:
        t = rows[0].get("transcript") or ""
        print(f"  transcript column: {len(t)} chars, first 300:")
        print("  " + t[:300].replace("\n", "\n  "))

    write("shape_report.md", shape_report(by_session))
    print(f"\nWrote {jsonl} and {OUT / 'shape_report.md'}")


# ── C + D: assumption tests (these WRITE) ───────────────────────────────────


class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, ok: bool | None, detail: str) -> None:
        mark = "PASS" if ok else ("FAIL" if ok is False else "INFO")
        self.rows.append((mark, name, detail))
        print(f"  [{mark}] {name}: {detail}")

    def render(self) -> str:
        lines = ["# Conductor API assumption tests", "", f"Generated {utcnow()}", ""]
        lines += ["| Result | Assumption | Detail |", "|---|---|---|"]
        for mark, name, detail in self.rows:
            lines.append(f"| {mark} | {name} | {detail.replace('|', '\\|')} |")
        return "\n".join(lines)


async def cmd_assume(p: Probe, session_id: str) -> None:
    r = Results()
    print(f"\nRunning assumption tests against session {session_id}")
    print("(this sends real prompts — make sure it's a scratch session)\n")

    baseline = await p.all_messages(session_id)
    base_max = max((m.get("sessionIndex", -1) for m in baseline), default=-1)
    last_id = baseline[-1]["id"] if baseline else None
    print(f"Baseline: {len(baseline)} messages, max sessionIndex={base_max}\n")

    # --- 3: after=<valid id> semantics
    if last_id:
        _, body = await p.messages(session_id, after=last_id, limit=100)
        data = body.get("data", [])
        r.add(
            "3a after=<last id> is exclusive",
            len(data) == 0 or all(m["id"] != last_id for m in data),
            f"returned {len(data)} messages, none of them the cursor",
        )
        if len(baseline) >= 3:
            mid = baseline[len(baseline) // 2]
            _, body = await p.messages(session_id, after=mid["id"], limit=100)
            data = body.get("data", [])
            idxs = [m.get("sessionIndex") for m in data]
            r.add(
                "3b after= returns ascending sessionIndex",
                idxs == sorted(idxs),
                f"{len(idxs)} messages, ascending={idxs == sorted(idxs)}",
            )
            r.add(
                "3c after= is strictly greater than the cursor index",
                all(i > mid["sessionIndex"] for i in idxs if i is not None),
                f"cursor idx={mid['sessionIndex']}, min back={min(idxs, default=None)}",
            )
            _, body = await p.messages(session_id, after=mid["id"], limit=2)
            n_back = len(body.get("data", []))
            r.add(
                "3d after= respects limit and sets hasMore",
                n_back <= 2,
                f"limit=2 returned {n_back}, hasMore={body.get('hasMore')}",
            )

    # --- 4: after=<garbage>
    status, body = await p.messages(
        session_id,
        after="msg_definitely_not_a_real_id",
        limit=10,
        raise_for_status=False,
    )
    n = len(body.get("data", [])) if isinstance(body, dict) else -1
    r.add(
        "4 after=<garbage id> behaviour",
        None,
        f"HTTP {status}, {n} messages returned "
        f"({'FULL REPLAY RISK' if n >= len(baseline) > 0 else 'no replay'})",
    )

    # --- 5: after=<id from a different session>
    _, sql = await p.req(
        "POST",
        "/sql",
        json={
            "query": (
                "SELECT session_id FROM session_transcripts_view "
                f"WHERE session_id <> '{session_id}' "
                "ORDER BY transcript_updated_at DESC LIMIT 1"
            )
        },
    )
    other_rows = sql.get("rows", [])
    if other_rows:
        other_sid = other_rows[0]["session_id"]
        _, other_body = await p.messages(other_sid, limit=1)
        other = other_body.get("data", [])
        if other:
            status, body = await p.messages(
                session_id, after=other[0]["id"], limit=10, raise_for_status=False
            )
            n = len(body.get("data", [])) if isinstance(body, dict) else -1
            r.add(
                "5 after=<id from another session>",
                None,
                f"HTTP {status}, {n} messages returned",
            )

    # --- 1 + 7: messageId round-trip and idempotency (THE linchpin)
    mid_key = str(uuid.uuid4())
    marker = f"probe-{mid_key[:8]}"
    prompt = f"Reply with exactly this token and nothing else: {marker}"

    print(f"\n  Posting prompt with messageId={mid_key} …")
    _, post1 = await p.req(
        "POST",
        f"/sessions/{session_id}/messages",
        json={"message": prompt, "messageId": mid_key},
    )
    r.add("post #1 accepted", None, json.dumps(post1))

    print("  Re-posting the SAME messageId …")
    status2, post2 = await p.req(
        "POST",
        f"/sessions/{session_id}/messages",
        json={"message": prompt, "messageId": mid_key},
        raise_for_status=False,
    )
    r.add(
        "post #2 (same messageId) response", None, f"HTTP {status2} {json.dumps(post2)}"
    )

    # --- D: timing trace while the turn runs
    print("\n  Tracing the turn (status @1s, messages @2s, 300s cap) …")
    trace: list[dict[str, Any]] = []
    t0 = time.monotonic()
    seen_working = False
    first_working_at: float | None = None
    finished_at: float | None = None
    cursor = last_id
    new_msgs: list[dict[str, Any]] = []

    for tick in range(300):
        elapsed = round(time.monotonic() - t0, 2)
        st = await p.get(f"/sessions/{session_id}/status")
        entry: dict[str, Any] = {"t": elapsed, "status": st.get("status")}
        if st.get("status") == "working" and not seen_working:
            seen_working = True
            first_working_at = elapsed
        if tick % 2 == 0:
            # No cursor means the session started empty — fall back to offset
            # paging from the baseline high-water mark.
            if cursor:
                _, mb = await p.messages(session_id, after=cursor, limit=100)
            else:
                _, mb = await p.messages(session_id, limit=100, offset=0)
            fresh = [
                m for m in mb.get("data", []) if m.get("sessionIndex", -1) > base_max
            ]
            if fresh:
                cursor = fresh[-1]["id"]
                new_msgs.extend(fresh)
                entry["new"] = [m.get("type") for m in fresh]
        trace.append(entry)

        if seen_working and st.get("status") == "idle":
            finished_at = elapsed
            # keep draining briefly to see if trailing content arrives after idle
            if elapsed - (first_working_at or 0) > 2 and tick > 4:
                break
        if elapsed > 300:
            break
        await asyncio.sleep(1)

    write("timing_trace.json", json.dumps(trace, indent=2))
    write("probe_turn_messages.jsonl", "\n".join(json.dumps(m) for m in new_msgs))

    r.add(
        "D1 `working` was observable",
        seen_working,
        f"first seen at {first_working_at}s"
        if seen_working
        else "NEVER seen — the status-only design would have missed this turn",
    )
    r.add(
        "D2 turn finished",
        finished_at is not None,
        f"idle after {finished_at}s" if finished_at else "still working at cap",
    )
    started = first_working_at if first_working_at is not None else 1e9
    pre_idle = [e["t"] for e in trace if e["status"] == "idle" and e["t"] < started]
    r.add(
        "D3 the queued-but-idle trap is real",
        None,
        (
            f"{len(pre_idle)} idle polls before the turn started — a naive "
            f"'idle means done' poller would have fired at t={pre_idle[0]}s"
        )
        if pre_idle
        else "no idle observed before working",
    )

    # --- 1: does our messageId appear as a transcript message id?
    ids = {m["id"] for m in new_msgs}
    r.add(
        "1 POST messageId appears as a transcript message id",
        mid_key in ids,
        f"messageId={mid_key} {'found' if mid_key in ids else 'NOT found'} "
        f"among {len(ids)} new message ids",
    )

    # --- 7: did the duplicate POST produce two prompts?
    echoes = [
        m
        for m in new_msgs
        if marker in json.dumps(m.get("content", ""))
        and str(m.get("type", "")).lower().find("user") >= 0
    ]
    any_echo = [m for m in new_msgs if marker in json.dumps(m.get("content", ""))]
    r.add(
        "7 re-POST with same messageId deduped",
        len(echoes) <= 1 if echoes else None,
        f"{len(echoes)} user-echo messages carry the marker "
        f"({len(any_echo)} messages total mention it). "
        "If this is 2, the idempotency-key design is INVALID.",
    )

    # --- 6: offset/hasMore stability
    _, a = await p.messages(session_id, limit=5, offset=0)
    _, b = await p.messages(session_id, limit=5, offset=0)
    r.add(
        "6 offset paging is stable across calls",
        [m["id"] for m in a.get("data", [])] == [m["id"] for m in b.get("data", [])],
        "two identical offset=0 calls returned the same ids",
    )

    # --- 8 is deliberately not automated: it needs a sleeping workspace.
    r.add(
        "8 POST to a sleeping workspace",
        None,
        "NOT AUTOMATED — re-run this script against a session whose workspace "
        "status is 'sleeping' and record whether the POST wakes it, errors, or hangs",
    )

    write("assumptions.md", r.render())
    print(f"\nWrote {OUT / 'assumptions.md'}")


# ── entrypoint ──────────────────────────────────────────────────────────────


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="READ-ONLY: dump transcripts + shape report")
    d.add_argument("--session", action="append", default=[], dest="sessions")
    d.add_argument("--auto", action="store_true", help="discover sessions via /sql")

    a = sub.add_parser("assume", help="WRITES: run API assumption tests")
    a.add_argument("--session", required=True, help="a SCRATCH session id")

    args = ap.parse_args()

    if not API_KEY:
        sys.exit(
            "CONDUCTOR_API_KEY is not set.\n"
            "Create one at https://app.conductor.build/users/api-keys and export it."
        )

    async with httpx.AsyncClient(
        base_url=API_URL,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(60.0, connect=10.0),
    ) as client:
        p = Probe(client)

        me = await p.get("/me")
        print(f"Authenticated: {json.dumps(me)}\n")
        write("me.json", json.dumps(me, indent=2))

        if args.cmd == "dump":
            await cmd_dump(p, args.sessions, args.auto)
        else:
            await cmd_assume(p, args.session)

        write("api_calls.json", json.dumps(p.calls, indent=2))
        print(f"\n{len(p.calls)} API calls logged to {OUT / 'api_calls.json'}")


if __name__ == "__main__":
    asyncio.run(main())
