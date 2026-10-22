#!/usr/bin/env python3
"""One scenario on the real game and on stellar-render at the same time: the same commands to
both (the protocol of docs/render.md section 9, which view.sh's game speaks too), each side's
replies, times, states, frames, gamestates and profiles, and a report of where they differ.

    qa.py run SCENARIO OUT_DIR [--render BINARY] [--save SAVE] [--only game|render]
    qa.py report OUT_DIR                       the report again from a run's records

A scenario is the protocol's commands, one a line (# comments), with `{OUT}` replaced by each
side's own folder (OUT_DIR/game, OUT_DIR/render), and these directives:

    @save PATH          the save both load (the game boots with it; the renderer gets `load-sim`);
                        default /opt/stellaris-saves/autosave_2210.01.01.sav
    @game CMD           a command to the game only; @render CMD to the renderer only
    @gamestate NAME     each side's simulation written ({OUT}/NAME.gamestate), and the two
                        compared with the sim's examples/diff (fields equal, the stores that differ)
    @prof NAME          each side's profile to {OUT}/prof_NAME.txt (`prof report`), then reset
    @mark NAME          a line in the timeline (a phase of the scenario)

Each command goes to both sides at once (a thread each) and the next one waits for both replies,
so both run the same sequence in step; a side's error is recorded and the scenario goes on (an
error reply to `load` stops it). The processes are started here on free ports, each in its own
work dir, and quit at the end.

OUT_DIR gets: report.md (what differs, the slowest commands, the profiles side by side),
timeline.tsv (a line per command and side: index, side, command, ms, reply), states.jsonl (each
`state` reply of both), run.json (the machine, its load, the builds, the times), the sides' logs,
and game/ and render/ (frames, gamestates, profiles).

Environment: QA_GAME_VIEW (default: view.sh next to this tool's folder), QA_RENDER (the
renderer's binary), QA_GAME_DATA (the renderer's --game, default /opt/stellaris), QA_DIFF (the
sim's examples/diff), QA_BOOT_TIMEOUT (seconds, default 300).
"""

import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ctl import Client  # noqa: E402

DEFAULT_SAVE = "/opt/stellaris-saves/autosave_2210.01.01.sav"
SIDES = ("game", "render")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def loadavg():
    try:
        with open("/proc/loadavg") as f:
            return f.read().split()[:3]
    except OSError:
        return None


class Side:
    """A process serving the protocol, and its client."""

    def __init__(self, name, argv, env, port, log_path):
        self.name, self.port = name, port
        self.log = open(log_path, "wb")
        self.proc = subprocess.Popen(argv, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        self.client = None
        self.t_start = time.time()

    def connect(self, timeout):
        """Waits until the side accepts a connection and answers `state`; returns seconds."""
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} exited with {self.proc.returncode} before serving")
            try:
                self.client = Client(f"127.0.0.1:{self.port}")
                reply, _, _ = self.client.send("state")
                if reply.startswith("ok"):
                    return time.time() - self.t_start
            except OSError:
                self.client = None
            time.sleep(1)
        raise RuntimeError(f"{self.name}: no answer on port {self.port} after {timeout} s")

    def send(self, command):
        try:
            reply, _, dt = self.client.send(command)
        except (OSError, ConnectionError) as e:
            return f"error connection: {e}", 0.0
        return reply, dt

    def quit(self):
        try:
            if self.client:
                self.client.send("quit")
        except (OSError, ConnectionError):
            pass
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, 9)
        self.log.close()


def start_sides(out, save, only, render_bin):
    view = os.environ.get("QA_GAME_VIEW", os.path.join(os.path.dirname(HERE), "view.sh"))
    data = os.environ.get("QA_GAME_DATA", "/opt/stellaris")
    sides = {}
    if only in (None, "game"):
        port = free_port()
        # the call-site table (the tick's phases) for `prof` on the game
        env = dict(os.environ, HEADLESS_KEEP="1", HEADLESS_PROFILE=os.environ.get("HEADLESS_PROFILE", "1"))
        sides["game"] = Side("game", ["bash", view, "--listen", str(port), save], env, port,
                             os.path.join(out, "game.log"))
    if only in (None, "render"):
        port = free_port()
        sides["render"] = Side("render", [render_bin, "--game", data, "--listen", str(port)],
                               dict(os.environ), port, os.path.join(out, "render.log"))
    return sides


def gamestate_diff(out, name):
    """examples/diff of the two sides' states at the same date (0 days run: the renderer's save
    loaded into the sim, compared store by store with the game's gamestate): a dict or None."""
    diff = os.environ.get("QA_DIFF")
    game, render = os.path.join(out, "game", f"{name}.gamestate"), os.path.join(out, "render", f"{name}.sav")
    if not diff or not (os.path.exists(game) and os.path.exists(render)):
        return None
    # the game writes CRLF (run.sh turns it into LF too)
    with open(game, "rb") as f:
        data = f.read()
    if b"\r\n" in data:
        with open(game, "wb") as f:
            f.write(data.replace(b"\r\n", b"\n"))
    path = os.path.join(out, f"diff_{name}.txt")
    env = dict(os.environ, STELLARIS_DIR=os.environ.get("QA_GAME_DATA", "/opt/stellaris"))
    with open(path, "wb") as f:
        r = subprocess.run([diff, render, game, "20"], stdout=f, stderr=subprocess.STDOUT, timeout=1800, env=env)
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    # `diff: N fields equal, M differ (P% equal) ...`, and the stores with the most fields differing
    overall = [l.strip() for l in lines if l.startswith("diff: ") and "fields equal" in l]
    stores = []
    for l in lines:
        p = l.split()
        if "fields" in p and "differ" in p and l.startswith("  "):
            try:
                stores.append((int(p[p.index("differ") - 1]), p[0]))
            except ValueError:
                pass
    stores.sort(reverse=True)
    worst = ", ".join(f"{s} {n}" for n, s in stores[:6] if n)
    summary = overall[-1:] + ([f"most differing: {worst}"] if worst else [])
    return {"rc": r.returncode, "summary": summary, "report": os.path.basename(path)}


def run(scenario, out, render_bin, save, only):
    os.makedirs(out, exist_ok=True)
    for s in SIDES:
        os.makedirs(os.path.join(out, s), exist_ok=True)
    with open(scenario, encoding="utf-8") as f:
        lines = [l.rstrip("\n") for l in f]
    steps = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    for l in steps:
        if l.startswith("@save "):
            save = l.split(None, 1)[1]
    meta = {"scenario": os.path.abspath(scenario), "save": save, "host": socket.gethostname(),
            "started": time.strftime("%Y-%m-%d %H:%M:%S"), "load_before": loadavg(),
            "render": render_bin, "only": only}
    sides = start_sides(out, save, only, render_bin)
    timeline = open(os.path.join(out, "timeline.tsv"), "w", encoding="utf-8")
    timeline.write("index\tside\tcommand\tms\treply\n")
    states = open(os.path.join(out, "states.jsonl"), "w", encoding="utf-8")
    results = {"boot": {}, "steps": [], "diffs": {}}
    try:
        boot = {}

        def boot_one(s):
            try:
                boot[s.name] = s.connect(int(os.environ.get("QA_BOOT_TIMEOUT", "300")))
            except RuntimeError as e:
                boot[s.name] = str(e)

        ts = [threading.Thread(target=boot_one, args=(s,)) for s in sides.values()]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        results["boot"] = boot
        for name, v in boot.items():
            timeline.write(f"-\t{name}\t(boot)\t{v * 1000 if isinstance(v, float) else -1:.0f}\t{v}\n")
        if any(not isinstance(v, float) for v in boot.values()):
            raise RuntimeError(f"boot: {boot}")
        if "render" in sides:
            reply, dt = sides["render"].send(f"load-sim {save}")
            timeline.write(f"-\trender\tload-sim {save}\t{dt * 1000:.0f}\t{reply}\n")
            if reply.startswith("error"):
                raise RuntimeError(f"render load-sim: {reply}")

        for i, step in enumerate(steps):
            words = step.split(None, 1)
            directive, rest = words[0], (words[1] if len(words) > 1 else "")
            if directive == "@save":
                continue
            targets, commands = list(sides), {}
            if directive == "@mark":
                timeline.write(f"{i}\t-\t@mark {rest}\t0\t\n")
                continue
            if directive in ("@game", "@render"):
                targets = [directive[1:]] if directive[1:] in sides else []
                cmd = rest
            elif directive == "@gamestate":
                cmd = None  # each side its own: below
            elif directive == "@prof":
                cmd = f"prof report {{OUT}}/prof_{rest}.txt"
            elif step.startswith("load "):
                continue  # both are loaded at the start
            else:
                cmd = step
            for s in targets:
                if directive == "@gamestate":
                    # the game's own gamestate writer; the renderer's sim as a save (docs/saves.md)
                    c = (f"gamestate {{OUT}}/{rest}.gamestate" if s == "game" else f"save {{OUT}}/{rest}.sav")
                elif directive == "@prof" and s == "render":
                    # the simulation's phases (the game's `prof` is its tick's phases)
                    c = f"simprof report {{OUT}}/prof_{rest}.tsv"
                else:
                    c = cmd
                commands[s] = c.replace("{OUT}", os.path.join(out, s))
            replies = {}

            def one(s):
                replies[s] = sides[s].send(commands[s])

            ts = [threading.Thread(target=one, args=(s,)) for s in targets]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            rec = {"index": i, "command": step, "sides": {}}
            for s in targets:
                reply, dt = replies[s]
                rec["sides"][s] = {"reply": reply, "ms": round(dt * 1000, 1)}
                timeline.write(f"{i}\t{s}\t{commands[s]}\t{dt * 1000:.1f}\t{reply[:2000]}\n")
                if directive == "state" and reply.startswith("ok"):
                    states.write(json.dumps({"index": i, "side": s, "state": reply[3:]}) + "\n")
            results["steps"].append(rec)
            if directive == "@prof" and "game" in targets:
                sides["game"].send("prof reset")  # the renderer's report resets itself
            if directive == "@gamestate":
                results["diffs"][rest] = gamestate_diff(out, rest)
            timeline.flush()
    finally:
        for s in sides.values():
            s.quit()
        timeline.close()
        states.close()
        meta["load_after"] = loadavg()
        meta["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(out, "run.json"), "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "results": results}, f, indent=1)
    report(out)
    return 0


def report(out):
    with open(os.path.join(out, "run.json"), encoding="utf-8") as f:
        run = json.load(f)
    meta, res = run["meta"], run["results"]
    w = []
    w.append(f"# QA run: {os.path.basename(meta['scenario'])}\n")
    w.append(f"{meta['started']} to {meta.get('finished')} on {meta['host']}, load average "
             f"{' '.join(meta['load_before'] or [])} before, {' '.join(meta.get('load_after') or [])} after. "
             f"Save `{meta['save']}`.\n")
    w.append("| side | boot |\n|---|---:|")
    for s, v in res["boot"].items():
        w.append(f"| {s} | {v:.1f} s |" if isinstance(v, (int, float)) else f"| {s} | {v} |")
    w.append("")
    errors = [(r, s, v) for r in res["steps"] for s, v in r["sides"].items() if v["reply"].startswith("error")]
    w.append(f"## Errors ({len(errors)})\n")
    for r, s, v in errors:
        w.append(f"- {r['index']} `{r['command']}`, {s}: {v['reply'][:300]}")
    w.append("")
    both = [r for r in res["steps"] if len(r["sides"]) == 2]
    w.append("## Times, command by command (ms; the game | the renderer)\n")
    w.append("| # | command | game | render | render / game |\n|---:|---|---:|---:|---:|")
    for r in both:
        g, x = r["sides"]["game"]["ms"], r["sides"]["render"]["ms"]
        ratio = f"{x / g:.2f}" if g > 0 else ""
        w.append(f"| {r['index']} | `{r['command'][:60]}` | {g:.1f} | {x:.1f} | {ratio} |")
    w.append("")
    if res.get("diffs"):
        w.append("## Gamestates compared (examples/diff: the renderer's against the game's)\n")
        for name, d in res["diffs"].items():
            if not d:
                w.append(f"- {name}: not compared (a side wrote none, or QA_DIFF unset)")
            else:
                w.append(f"- {name}: {'; '.join(d['summary'])} ([{d['report']}]({d['report']}))")
        w.append("")
    script = [r for r in both if r["command"].split()[0] in ("trigger", "effect")]
    if script:
        def result(reply):
            # the game: "ok trigger MS ms: Trigger returned: TRUE"; the renderer: "ok trigger MS ms: yes"
            if reply.startswith("error"):
                return "error", None
            words = reply.split()
            ms = float(words[2]) if len(words) > 2 and words[3:4] == ["ms:"] else None
            text = reply.split("ms:", 1)[-1].strip().lower()
            if words[1] == "trigger":
                return ("yes" if text in ("yes", "trigger returned: true") else
                        "no" if text in ("no", "trigger returned: false") else text), ms
            return "done", ms
        mismatches = 0
        rows = []
        for r in script:
            (g, gms), (x, xms) = result(r["sides"]["game"]["reply"]), result(r["sides"]["render"]["reply"])
            same = g == x
            mismatches += not same
            rows.append(f"| {r['index']} | `{r['command'][:70]}` | {g} | {x} | {'' if same else '**differs**'} | "
                        f"{'' if gms is None else f'{gms:.3f}'} | {'' if xms is None else f'{xms:.3f}'} |")
        w.append(f"## Script ({len(script)} statements, {mismatches} results differ)\n")
        w.append("The handler's own time (ms): the game's console handler (parsing, checking, running), the "
                 "simulation's evaluator.\n")
        w.append("| # | statement | game | render | | game ms | render ms |\n|---:|---|---|---|---|---:|---:|")
        w.extend(rows)
        w.append("")
    for r in res["steps"]:
        if not r["command"].startswith("@prof "):
            continue
        name = r["command"].split(None, 1)[1]
        w.append(f"## Profile `{name}`\n")
        for s, v in r["sides"].items():
            w.append(f"- {s}: `{v['reply'][:300]}`")
        gp = os.path.join(out, "game", f"prof_{name}.txt")
        rp = os.path.join(out, "render", f"prof_{name}.tsv")
        days = None
        greply = r["sides"].get("game", {}).get("reply", "")
        gw = greply.split()
        if "ticks" in gw:
            days = int(gw[gw.index("ticks") - 1]) / 10
        rows, phases = [], {}
        if os.path.exists(gp):
            with open(gp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    p = line.rstrip("\n").split("\t")
                    if len(p) >= 9 and p[0] != "site" and not line.startswith("#"):
                        rows.append((float(p[7]), float(p[6]), float(p[8]), p[3], p[1], p[2], p[4]))
                        if p[1].startswith("CGameState::HandleTurnTick"):
                            for kind, fn in (("micro", "MicroUpdate"), ("ai", "ProcessAI"), ("daily", "DailyUpdate"),
                                             ("monthly", "MonthlyUpdate"), ("yearly", "YearlyUpdate")):
                                if p[2].startswith(f"CGameState::{fn}("):
                                    phases.setdefault(kind, [0.0, 0.0])[0] += float(p[6])
        sim_rows = []
        if os.path.exists(rp):
            with open(rp, encoding="utf-8", errors="replace") as f:
                for line in f.read().splitlines()[1:]:
                    p = line.split("\t")
                    if len(p) >= 5:
                        sim_rows.append((float(p[2]) * 1000, int(p[1]), float(p[3]), int(p[4]), p[0]))
                        phases.setdefault(p[0].split(".")[0], [0.0, 0.0])[1] += float(p[2]) * 1000
            sim_rows.sort(reverse=True)
        if phases and days:
            w.append(f"\nThe tick's phases, ms a day over {days:g} days (the game's inclusive time; the "
                     "simulation's units by kind):\n")
            w.append("| phase | game | render | render / game |\n|---|---:|---:|---:|")
            for kind in sorted(phases, key=lambda k: -max(phases[k])):
                g, x = phases[kind][0] / days, phases[kind][1] / days
                w.append(f"| {kind} | {g:.2f} | {x:.2f} | {(x / g) if g else 0:.1f} |")
        if rows:
            rows.sort(reverse=True)
            w.append("\nThe game's call sites by self time (the tick's phases; ms in the profile, ms a day "
                     "inclusive):\n")
            w.append("| self ms | ms | ms a day | calls | caller | callee |\n|---:|---:|---:|---:|---|---|")
            for selfms, ms, perday, calls, caller, callee, _ in rows[:25]:
                w.append(f"| {selfms:.1f} | {ms:.1f} | {perday:.2f} | {calls} | `{caller[:50]}` | `{callee[:60]}` |")
        if sim_rows:
            w.append("\nThe simulation's units by time (`simprof`):\n")
            w.append("| ms | ms a day | calls | ms a call | draws | unit |\n|---:|---:|---:|---:|---:|---|")
            for ms, calls, per, draws, unit in sim_rows[:25]:
                w.append(f"| {ms:.1f} | {ms / days if days else 0:.2f} | {calls} | {per:.3f} | {draws} | `{unit}` |")
        w.append("")
    w.append("## Replies that differ\n")
    for r in both:
        g, x = r["sides"]["game"]["reply"], r["sides"]["render"]["reply"]
        if r["command"].split()[0] in ("state", "frame", "trigger", "effect", "@prof", "@gamestate", "prof"):
            continue
        if g != x:
            w.append(f"- {r['index']} `{r['command'][:60]}`\n  - game: `{g[:200]}`\n  - render: `{x[:200]}`")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(w) + "\n")
    print(os.path.join(out, "report.md"))


def main(argv):
    if len(argv) >= 2 and argv[0] == "report":
        report(argv[1])
        return 0
    if len(argv) < 3 or argv[0] != "run":
        print(__doc__)
        return 2
    opts = {"--render": os.environ.get("QA_RENDER"), "--save": DEFAULT_SAVE, "--only": None}
    rest, i = [], 1
    while i < len(argv):
        if argv[i] in opts and i + 1 < len(argv):
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    if len(rest) != 2 or (opts["--only"] != "game" and not opts["--render"]):
        print(__doc__)
        return 2
    return run(rest[0], os.path.abspath(rest[1]), opts["--render"], opts["--save"], opts["--only"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
