"""ch-11 follow-up: what the JSON format itself costs, on fixed trajectories.

The g6 / g7 cost benches measured typed on against typed off and found
+40–59% prompt tokens, but two councils never take the same path, so that
delta mixes the format with the behaviour it causes (more tool turns, a
longer researcher history) and with run-to-run noise. This replay holds the
trajectory fixed: every recorded ``llm_call`` of a typed-on council is
rebuilt as the same request in the prose format —

- the role's result tool (``council_report`` …) taken off ``tools``;
- the ``RESULT.`` instruction (``typed_results.result_instruction``) taken
  out of the prompt;
- every JSON record in a message (``typed_results.dumps``) replaced by
  ``typed_results.render`` of the same record, the prose the role handed
  on before G7;
- a result-tool call in an assistant turn (a re-asked record) replaced by
  its rendering;

and the difference is counted at that call's own measured rate
(``prompt_tokens`` over the request's characters). The completion side
compares each record's arguments with their rendering at the same rate.
No model is called.

What it does not remove: tools the typed arm adds besides the result tool
(listed per role as ``extra tools``), and anything the format changed
about what the role did next. ``on − format`` is the typed council as it
ran, in prose; set against the off arm, the rest is behaviour plus noise.

  python benchmarks/consultants/typed_format_replay.py \\
      --bench DIR [DIR ...] --out benchmarks/consultants/results/<date>/typed-format-replay

Each bench dir holds ``t<n>-on`` / ``t<n>-off`` projects with their
council under ``.claude-hooks/consultants/csl-*/transcript.db``.
``report.md`` and ``metadata.json`` hold counts only.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from consultants.engine import typed_results as tr  # noqa: E402

RECORD_START = re.compile(r'\{\s*"record":\s*"(council_\w+)"')


def _chars(messages: list, tools: list) -> int:
    n = len(json.dumps(tools, ensure_ascii=False)) if tools else 0
    for m in messages:
        n += len(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            n += len(fn.get("name") or "") + len(_arg_text(fn.get("arguments")))
    return n


def _arg_text(a) -> str:
    return a if isinstance(a, str) else json.dumps(a or {}, ensure_ascii=False)


def _swap_records(text: str) -> tuple[str, int]:
    """``text`` with every tagged JSON record rendered as prose."""
    out, i, n = [], 0, 0
    for m in RECORD_START.finditer(text):
        if m.start() < i:
            continue
        try:
            d, end = json.JSONDecoder().raw_decode(text, m.start())
        except ValueError:
            continue
        name = d.get(tr.RECORD_KEY)
        rec = {k: v for k, v in d.items() if k != tr.RECORD_KEY}
        try:
            prose = tr.render(name, rec)
        except (KeyError, TypeError):
            continue
        out += [text[i:m.start()], prose]
        i, n = end, n + 1
    out.append(text[i:])
    return "".join(out), n


def as_prose(messages: list, tools: list) -> tuple[list, list, int]:
    """The request in the prose format, and how many records it swapped."""
    names = set(tr.RESULT_NAMES)
    tools2 = [t for t in tools or []
              if (t.get("function") or {}).get("name") not in names]
    swapped, msgs2 = 0, []
    for m in messages:
        m2 = dict(m)
        c = m2.get("content") or ""
        for name in tr.RESULT_NAMES:
            c = c.replace(tr.result_instruction(name), "")
        c, k = _swap_records(c)
        swapped += k
        m2["content"] = c
        calls = []
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if fn.get("name") in names:
                rec = tr.parse(fn["name"], fn.get("arguments"))
                if rec is not None:
                    m2["content"] = (m2["content"] or "") + tr.render(fn["name"], rec)
                    swapped += 1
                    continue
            calls.append(tc)
        if m.get("tool_calls"):
            m2["tool_calls"] = calls
        msgs2.append(m2)
    return msgs2, tools2, swapped


def _response_record(resp: dict) -> tuple[int, int]:
    """(argument chars, rendered chars) of a result-tool call in the reply."""
    msg = ((resp.get("choices") or [{}])[0].get("message")
           or resp.get("message") or {})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        if fn.get("name") in tr.RESULT_NAMES:
            raw = _arg_text(fn.get("arguments"))
            rec = tr.parse(fn["name"], raw)
            if rec is not None:
                return len(raw), len(tr.render(fn["name"], rec))
    return 0, 0


def _tool_names(tools) -> set:
    return {(t.get("function") or {}).get("name") for t in tools or []}


def council(db: Path) -> dict:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    roles = defaultdict(lambda: defaultdict(float))
    tools_by_role = defaultdict(set)
    for role, rq, rs, pt, ct in c.execute(
            "select role, request_json, response_json, prompt_tokens, "
            "completion_tokens from events where kind='llm_call' "
            "order by rowid"):
        r = roles[role]
        pt, ct = int(pt or 0), int(ct or 0)
        r["calls"] += 1
        r["prompt"] += pt
        r["completion"] += ct
        try:
            req = json.loads(rq) if rq else {}
            resp = json.loads(rs) if rs else {}
        except ValueError:
            r["unparsed"] += 1
            continue
        msgs, tools = req.get("messages") or [], req.get("tools") or []
        tools_by_role[role] |= _tool_names(tools)
        chars = _chars(msgs, tools)
        if not chars or not pt:
            continue
        rate = pt / chars
        m2, t2, k = as_prose(msgs, tools)
        r["format_prompt"] += (chars - _chars(m2, t2)) * rate
        r["records_in_prompt"] += k
        a, p = _response_record(resp)
        if a:
            r["records_out"] += 1
            r["format_completion"] += (a - p) * rate
            r["record_chars"] += a
            r["rendered_chars"] += p
    c.close()
    return {"roles": {k: dict(v) for k, v in roles.items()},
            "tools": {k: sorted(x for x in v if x) for k, v in tools_by_role.items()}}


def _db(project: Path) -> Path | None:
    dbs = sorted(project.glob(".claude-hooks/consultants/csl-*/transcript.db"))
    return dbs[0] if dbs else None


def _sum(cs: list[dict], key: str, role: str | None = None) -> float:
    return sum(v.get(key, 0) for c in cs for r, v in c["roles"].items()
               if role is None or r == role)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bench", nargs="+", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    meta, lines = {}, ["# Typed format cost on fixed trajectories", ""]
    for bench in a.bench:
        arms = {"on": [], "off": []}
        pairs = []
        for on in sorted(bench.glob("t*-on")):
            off = on.with_name(on.name[:-2] + "off")
            d_on, d_off = _db(on), _db(off)
            if not (d_on and d_off):
                continue
            con, coff = council(d_on), council(d_off)
            arms["on"].append(con)
            arms["off"].append(coff)
            pairs.append((on.name[:-3], con, coff))
        if not pairs:
            continue
        on_p, off_p = _sum(arms["on"], "prompt"), _sum(arms["off"], "prompt")
        on_c, off_c = _sum(arms["on"], "completion"), _sum(arms["off"], "completion")
        fp, fc = _sum(arms["on"], "format_prompt"), _sum(arms["on"], "format_completion")
        b = {"trials": len(pairs), "on_prompt": on_p, "off_prompt": off_p,
             "on_completion": on_c, "off_completion": off_c,
             "format_prompt": round(fp), "format_completion": round(fc),
             "pairs": [], "roles": {}}
        lines += [f"## {bench.name} ({len(pairs)} trials per arm)", "",
                  "| | prompt | completion |", "|---|---|---|",
                  f"| off, measured | {off_p:,.0f} | {off_c:,.0f} |",
                  f"| on, measured | {on_p:,.0f} | {on_c:,.0f} |",
                  f"| on, replayed in prose | {on_p - fp:,.0f} | {on_c - fc:,.0f} |",
                  f"| **format** (on − replayed) | **{fp:,.0f} "
                  f"({fp / on_p:.1%} of on)** | **{fc:,.0f} ({fc / on_c:.1%} of on)** |",
                  f"| on vs off, measured | {on_p / off_p - 1:+.1%} | {on_c / off_c - 1:+.1%} |",
                  f"| replayed vs off (behaviour + noise) | {(on_p - fp) / off_p - 1:+.1%} "
                  f"| {(on_c - fc) / off_c - 1:+.1%} |",
                  f"| share of the on−off delta that is format | "
                  f"{fp / (on_p - off_p):.0%} | "
                  f"{(fc / (on_c - off_c)) if on_c != off_c else 0:.0%} |", "",
                  "| role | calls off / on | prompt off / on | format prompt | "
                  "records in prompts | records out | record / rendered chars | extra tools on |",
                  "|---|---|---|---|---|---|---|---|"]
        for role in sorted({r for c in arms["on"] + arms["off"] for r in c["roles"]}):
            t_on = set().union(*(set(c["tools"].get(role, [])) for c in arms["on"]))
            t_off = set().union(*(set(c["tools"].get(role, [])) for c in arms["off"]))
            extra = sorted(t_on - t_off - set(tr.RESULT_NAMES))
            rc, rr = _sum(arms["on"], "record_chars", role), _sum(arms["on"], "rendered_chars", role)
            row = {"calls_off": _sum(arms["off"], "calls", role),
                   "calls_on": _sum(arms["on"], "calls", role),
                   "prompt_off": _sum(arms["off"], "prompt", role),
                   "prompt_on": _sum(arms["on"], "prompt", role),
                   "format_prompt": round(_sum(arms["on"], "format_prompt", role)),
                   "records_in_prompt": _sum(arms["on"], "records_in_prompt", role),
                   "records_out": _sum(arms["on"], "records_out", role),
                   "record_chars": rc, "rendered_chars": rr, "extra_tools": extra}
            b["roles"][role] = row
            lines.append(
                f"| {role} | {row['calls_off']:.0f} / {row['calls_on']:.0f} "
                f"| {row['prompt_off']:,.0f} / {row['prompt_on']:,.0f} "
                f"| {row['format_prompt']:,} | {row['records_in_prompt']:.0f} "
                f"| {row['records_out']:.0f} | "
                f"{(f'{rc:,.0f} / {rr:,.0f}') if rc else '–'} "
                f"| {', '.join(extra) or '–'} |")
        lines += ["", "| trial | prompt off | prompt on | format | on/off | replayed/off |",
                  "|---|---|---|---|---|---|"]
        for name, con, coff in pairs:
            p1, p0 = _sum([con], "prompt"), _sum([coff], "prompt")
            f = _sum([con], "format_prompt")
            b["pairs"].append({"trial": name, "off": p0, "on": p1, "format": round(f)})
            lines.append(f"| {name} | {p0:,.0f} | {p1:,.0f} | {f:,.0f} "
                         f"| {p1 / p0:.2f} | {(p1 - f) / p0:.2f} |")
        lines.append("")
        meta[bench.name] = b
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "metadata.json").write_text(json.dumps(meta, indent=1))
    (a.out / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
