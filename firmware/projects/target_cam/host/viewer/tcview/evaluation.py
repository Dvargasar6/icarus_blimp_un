"""
Scoring a detector on labelled sessions, and the verdicts drawn from it.

The numbers are eval_frames.py's, from the same functions (labels.score through
the shared C library), kept per frame so the Verify tab can show which frames
fail and draw them. What is added is the judgement, written once so a single
evaluation and a comparison cannot disagree.

A detector here is a model set at a stage (Candidate): stage 0 is the HSV test
with each frame's own saved settings (stage 0 is calibrated by hand, so the
settings that were live when the frame was taken are the ones to score), 1 the
set's colour table, 2 the table and its classifier, 3 all three. From stage 2
up the stages remove blobs, not pixels, so the pixel figures only tell stages
0 and 1 apart; the blob figures (TP/FN/FP, false blob 1) are what decide.

One detector (README "Retraining the colour table", step 4): acceptable on the
chosen folders only if every balloon is found (FN 0) and no frame without a
balloon gets a false blob 1. Blob 1 is what the blimp steers to, so one false
blob 1 fails it however good the rest is.

Two detectors (step 4, "if one figure improves and another gets worse, keep
the old table"): B is better than A when it is at least as good on every
session in every figure (false blob 1, FN, FP, pixel F1) and strictly better in
one; the mirror image makes A better; all equal is a tie; anything mixed is
inconclusive. A false blob 1 decides alone when the other has none and finds
as many balloons. And a decision resting on a single frame of a small set is
inconclusive too: with tens of frames one frame moves recall by several points
(learned-detector report §7), which a different set of frames could undo.

Warnings travel with the verdict: folders the detector was trained on give
optimistic figures (recognised by the paths its .json records).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from . import detector as D
from . import labels as L
from . import library as Lib
from .library import ModelSet, Session

# Pixel F1 differences below this (points) are noise: one frame's rim pixels.
PX_F1_TOL = 1.0
# Below these counts a one-frame difference is not a result (see above).
FEW_FRAMES = 30


@dataclass
class Candidate:
    """What is evaluated: a model set at a stage, or stage 0 (no set)."""
    mset: Optional[ModelSet]
    stage: int

    @property
    def label(self) -> str:
        if self.stage == 0:
            return "stage 0 (HSV, each frame's settings)"
        return "%s · stage %d" % (self.mset.name, self.stage)

    def models(self) -> D.TcModels:
        if self.stage == 0:
            return D.TcModels()
        s = self.mset
        if self.stage not in s.stages:
            raise D.ModelError("%s cannot run stage %d (it offers %s)"
                               % (s.name, self.stage, s.stages or "nothing"))
        if self.stage >= 3:
            raise D.ModelError("stage 3 is not built yet (phase P7)")
        return D.models(s.bin("lut"), s.bin("clf") if self.stage >= 2 else None)

    def trained_on(self) -> list[tuple[str, list, list]]:
        """(kind, folders, names) of every model this candidate runs. Models
        from before the trainers recorded paths give names only."""
        if self.stage == 0:
            return []
        return [(k, self.mset.session_paths(k), self.mset.sessions(k))
                for k in Lib.KINDS[:self.stage]]


# ---- per frame -------------------------------------------------------------------

@dataclass
class Blob:
    cx: float
    cy: float
    x0: int
    y0: int
    x1: int
    y1: int
    area: int
    verdict: str            # "balloon" | "false"
    conf: float = -1.0      # stage >= 2; -1 below


@dataclass
class FrameEval:
    session: str            # Session.key
    sf: L.SavedFrame
    lab: np.ndarray
    mask: np.ndarray
    blobs: list
    score: L.FrameScore

    @property
    def name(self) -> str:
        return self.score.name

    @property
    def has_balloon(self) -> bool:
        return self.score.n_balloons > 0

    def outcome(self) -> str:
        """One word for the row colour: "ok" or what went wrong, worst first."""
        s = self.score
        if s.b1 == "false":
            return "false b1"
        if not s.n_balloons:
            return "false" if s.fp else "ok"
        if s.fn and s.tp == 0:
            return "missed"
        if s.fn:
            return "partial"
        if s.fp:
            return "false"
        return "ok"


def eval_frame(sf: L.SavedFrame, lab: np.ndarray, m: D.TcModels, stage: int,
               session: str) -> FrameEval:
    st = sf.settings()
    r = D.pipeline(st, sf.frame.data, stage, m)
    if r.stage != stage:
        raise RuntimeError("stage %d was asked for and stage %d ran: a model is missing"
                           % (stage, r.stage))
    mask = D.mask(st, sf.frame.data, stage, m)
    cents = [(r.r.b[k].cx, r.r.b[k].cy) for k in range(r.r.n_report)]
    sc = L.score(sf.stem.name, lab, cents, mask, r.stage)
    bal = L.balloons(lab)
    blobs = []
    for k in range(r.r.n_report):
        b = r.r.b[k]
        hit = any(c.contains(b.cx, b.cy) for c in bal)
        blobs.append(Blob(b.cx, b.cy, b.x0, b.y0, b.x1, b.y1, b.area,
                          "balloon" if hit else "false", float(r.conf[k])))
    return FrameEval(session, sf, lab, mask, blobs, sc)


# ---- totals ----------------------------------------------------------------------

@dataclass
class Totals:
    frames: int = 0
    with_balloon: int = 0
    empty: int = 0
    balloons: int = 0
    tp: int = 0
    fn: int = 0
    fp: int = 0
    split: int = 0
    b1_ok: int = 0          # frames with a balloon whose blob 1 is one
    b1_none: int = 0        # frames with a balloon and no blob at all
    false_b1: int = 0       # frames without a balloon that got a blob 1
    px_tp: int = 0
    px_fp: int = 0
    px_fn: int = 0

    def add(self, f: FrameEval):
        s = f.score
        self.frames += 1
        self.balloons += s.n_balloons
        for k in ("tp", "fn", "fp", "split", "px_tp", "px_fp", "px_fn"):
            setattr(self, k, getattr(self, k) + getattr(s, k))
        if s.n_balloons:
            self.with_balloon += 1
            self.b1_ok += s.b1 == "balloon"
            self.b1_none += s.b1 == "none"
        else:
            self.empty += 1
            self.false_b1 += s.b1 == "false"

    @property
    def precision(self) -> float:
        return L.ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float:
        return L.ratio(self.tp, self.balloons)

    @property
    def px_precision(self) -> float:
        return L.ratio(self.px_tp, self.px_tp + self.px_fp)

    @property
    def px_recall(self) -> float:
        return L.ratio(self.px_tp, self.px_tp + self.px_fn)

    @property
    def px_f1(self) -> float:
        d = 2 * self.px_tp + self.px_fp + self.px_fn
        return 2 * self.px_tp / d if d else float("nan")


def pct(x: float) -> str:
    return "–" if math.isnan(x) else "%.1f %%" % (100 * x)


@dataclass
class EvalResult:
    cand: Candidate
    sessions: list
    frames: list
    skipped: int = 0        # saved frames without labels

    @property
    def label(self) -> str:
        return self.cand.label

    def totals(self, session: str | None = None) -> Totals:
        t = Totals()
        for f in self.frames:
            if session is None or f.session == session:
                t.add(f)
        return t

    def by_session(self) -> dict:
        return {s.key: self.totals(s.key) for s in self.sessions}

    def trained_on(self) -> list[str]:
        """Chosen sessions that a model of the candidate was trained on (or
        that hold, or sit inside, such a folder): their figures are optimistic."""
        out = []
        for s in self.sessions:
            p = s.path.resolve()
            for kind, used, names in self.cand.trained_on():
                if used:
                    if any(p == u or p.is_relative_to(u) or u.is_relative_to(p) for u in used):
                        out.append("%s (%s)" % (s.key, kind))
                        break
                elif s.name in names:
                    # A guess: another folder of the same name would match too.
                    out.append("%s (%s, by name)" % (s.key, kind))
                    break
        return out

    def text(self) -> str:
        """The totals in eval_frames.py's wording, for Copy."""
        t = self.totals()
        lines = ["%s on %s" % (self.label, ", ".join(s.key for s in self.sessions)),
                 "frames        %d labelled (%d with a balloon, %d without)%s" % (
                     t.frames, t.with_balloon, t.empty,
                     ", %d unlabelled skipped" % self.skipped if self.skipped else ""),
                 "blobs         TP %d  FN %d  FP %d  split %d   precision %s  recall %s" % (
                     t.tp, t.fn, t.fp, t.split, pct(t.precision), pct(t.recall)),
                 "blob 1        a balloon in %d of %d frames with one; a false blob 1 in %d of "
                 "%d frames without" % (t.b1_ok, t.with_balloon, t.false_b1, t.empty),
                 "pixels        precision %s  recall %s  F1 %s" % (
                     pct(t.px_precision), pct(t.px_recall), pct(t.px_f1))]
        for k, st in self.by_session().items():
            lines.append("  %-30s TP %d FN %d FP %d  false b1 %d/%d  px F1 %s" % (
                k, st.tp, st.fn, st.fp, st.false_b1, st.empty, pct(st.px_f1)))
        return "\n".join(lines)


def evaluate(cand: Candidate, sessions: list,
             progress: Optional[Callable[[int, int], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None) -> EvalResult:
    """Score cand on every labelled frame of the sessions (frame folders;
    blob sets cannot be scored). progress(done, total) per frame; cancelled()
    True stops early with what is done so far."""
    m = cand.models()
    stems = [(s, stem) for s in sessions if not s.is_blobset for stem in L.list_frames(s.path)]
    res = EvalResult(cand, [s for s in sessions if not s.is_blobset], [])
    for i, (s, stem) in enumerate(stems):
        if cancelled and cancelled():
            break
        sf = L.load_frame(stem)
        if not sf.label_path.exists():
            res.skipped += 1
        else:
            lab = L.load_labels(sf.label_path, sf.frame.w, sf.frame.h)
            res.frames.append(eval_frame(sf, lab, m, cand.stage, s.key))
        if progress:
            progress(i + 1, len(stems))
    return res


# ---- verdicts --------------------------------------------------------------------

@dataclass
class Verdict:
    ok: bool
    title: str
    reasons: list = field(default_factory=list)
    caveats: list = field(default_factory=list)
    tone: str = "ok"        # ok | warn | err | info

    def text(self) -> str:
        return "\n".join([self.title] + ["- " + r for r in self.reasons] +
                         ["! " + c for c in self.caveats])


def _small(t: Totals) -> bool:
    return t.frames < FEW_FRAMES


def judge(res: EvalResult) -> Verdict:
    """README step 4 on one detector: every balloon found, no false blob 1."""
    t = res.totals()
    if t.frames == 0:
        return Verdict(False, "No labelled frames",
                       ["None of the chosen folders has frames with a .labels.png."], tone="err")
    reasons, ok = [], True
    if t.false_b1:
        ok = False
        reasons.append("%d of %d frames without a balloon get a false blob 1: the blimp would "
                       "steer to a false target. Do not fly this." % (t.false_b1, t.empty))
    if t.fn:
        ok = False
        reasons.append("%d of %d balloons not found (FN)." % (t.fn, t.balloons))
    if t.fp:
        reasons.append("%d false blobs (FP) in all%s." % (
            t.fp, "" if t.false_b1 else ", none of them blob 1"))
    if ok:
        reasons.insert(0, "Every balloon found (%d/%d) and no false blob 1 on %d frames "
                       "without a balloon." % (t.tp, t.balloons, t.empty))
    caveats = []
    trained = res.trained_on()
    if trained:
        caveats.append("Trained on %s: the figures there are optimistic; they show gross "
                       "failures, not a score." % ", ".join(trained))
    if t.empty == 0:
        caveats.append("No frame without a balloon: false blob 1s were not tested.")
    if _small(t):
        caveats.append("Only %d frames: one frame moves recall by %.0f points." % (
            t.frames, 100.0 / max(t.balloons, 1)))
    if res.skipped:
        caveats.append("%d saved frames without labels were left out." % res.skipped)
    if res.cand.stage >= 2:
        caveats.append("Pixel figures are stage 1's: the classifier removes blobs, not pixels.")
    if ok:
        title = "Meets the README criteria on these folders"
        tone = "info" if (trained or _small(t)) else "ok"
    else:
        title = "Does not meet the README criteria"
        tone = "err" if t.false_b1 else "warn"
    return Verdict(ok, title, reasons, caveats, tone)


def _edges(a: Totals, b: Totals) -> tuple[list, list]:
    """What B does better than A (pros) and worse (cons), as short phrases."""
    pros, cons = [], []

    def lower(name, va, vb):
        if vb < va:
            pros.append("%s %d → %d" % (name, va, vb))
        elif vb > va:
            cons.append("%s %d → %d" % (name, va, vb))

    lower("false blob 1", a.false_b1, b.false_b1)
    lower("FN", a.fn, b.fn)
    lower("FP", a.fp, b.fp)
    fa, fb = a.px_f1, b.px_f1
    if not (math.isnan(fa) or math.isnan(fb)):
        d = 100 * (fb - fa)
        if d > PX_F1_TOL:
            pros.append("px F1 %.1f → %.1f" % (100 * fa, 100 * fb))
        elif d < -PX_F1_TOL:
            cons.append("px F1 %.1f → %.1f" % (100 * fa, 100 * fb))
    return pros, cons


@dataclass
class SessionDiff:
    session: str
    a: Totals
    b: Totals

    def better(self) -> str:
        """"B", "A", "tie" or "mixed" on this session alone."""
        pros, cons = _edges(self.a, self.b)
        if pros and not cons:
            return "B"
        if cons and not pros:
            return "A"
        return "mixed" if pros else "tie"


@dataclass
class Comparison:
    a: EvalResult
    b: EvalResult
    winner: str             # "A" | "B" | "tie" | "inconclusive"
    title: str
    reasons: list
    caveats: list
    sessions: list
    tone: str

    def text(self) -> str:
        return "\n".join(["A = %s" % self.a.label, "B = %s" % self.b.label, self.title] +
                         ["- " + r for r in self.reasons] + ["! " + c for c in self.caveats])

    def differing_frames(self, px_tol: float = 0.05) -> list:
        """(A's, B's) evaluation of the frames the two treat differently: another
        blob outcome, or pixel precision or recall apart by more than px_tol."""
        bya = {(f.session, f.name): f for f in self.a.frames}
        out = []
        for fb in self.b.frames:
            fa = bya.get((fb.session, fb.name))
            if fa is None:
                continue
            sa, sb = fa.score, fb.score
            blob = (fa.outcome() != fb.outcome() or sa.fn != sb.fn or sa.fp != sb.fp
                    or sa.b1 != sb.b1)
            px = any(abs(_nz(L.ratio(x, x + y)) - _nz(L.ratio(u, u + v))) > px_tol
                     for (x, y), (u, v) in (((sa.px_tp, sa.px_fp), (sb.px_tp, sb.px_fp)),
                                            ((sa.px_tp, sa.px_fn), (sb.px_tp, sb.px_fn))))
            if blob or px:
                out.append((fa, fb))
        return out


def _nz(x: float) -> float:
    return 0.0 if math.isnan(x) else x


def compare(a: EvalResult, b: EvalResult) -> Comparison:
    names = [s.key for s in a.sessions if s.key in {x.key for x in b.sessions}]
    diffs = [SessionDiff(n, a.totals(n), b.totals(n)) for n in names]
    caveats = []
    for tag, r in (("A", a), ("B", b)):
        tr = r.trained_on()
        if tr:
            caveats.append("%s was trained on %s: its figures there are optimistic."
                           % (tag, ", ".join(tr)))
    if {s.key for s in a.sessions} ^ {s.key for s in b.sessions}:
        caveats.append("Only the sessions evaluated with both are compared.")
    if not names:
        return Comparison(a, b, "inconclusive", "No session in common", [], caveats, [], "err")

    ta = sum_totals(d.a for d in diffs)
    tb = sum_totals(d.b for d in diffs)
    reasons = []
    if ta.false_b1 and not tb.false_b1 and tb.fn <= ta.fn:
        winner = "B"
        reasons.append("A puts a false blob 1 on %d frame(s) without a balloon and B on none, "
                       "without losing balloons (FN %d → %d)." % (ta.false_b1, ta.fn, tb.fn))
    elif tb.false_b1 and not ta.false_b1 and ta.fn <= tb.fn:
        winner = "A"
        reasons.append("B puts a false blob 1 on %d frame(s) without a balloon and A on none, "
                       "without losing balloons (FN %d → %d)." % (tb.false_b1, tb.fn, ta.fn))
    else:
        if ta.false_b1 and tb.false_b1:
            reasons.append("Both put a false blob 1 on some frame without a balloon (A %d, "
                           "B %d): neither should be flown as it is." % (ta.false_b1, tb.false_b1))
        pros_all, cons_all = [], []
        for d in diffs:
            pros, cons = _edges(d.a, d.b)
            pros_all += ["%s: %s" % (d.session, p) for p in pros]
            cons_all += ["%s: %s" % (d.session, c) for c in cons]
        if pros_all and not cons_all:
            winner = "B"
            reasons.append("B equals or beats A in every session and figure, and is better in: "
                           + "; ".join(pros_all) + ".")
        elif cons_all and not pros_all:
            winner = "A"
            reasons.append("A equals or beats B in every session and figure, and is better in: "
                           + "; ".join(cons_all) + ".")
        elif not pros_all and not cons_all:
            winner = "tie"
            reasons.append("Both give the same figures (px F1 within %.0f point) in every "
                           "session." % PX_F1_TOL)
        else:
            winner = "inconclusive"
            reasons.append("B is better in: " + "; ".join(pros_all) + ".")
            reasons.append("B is worse in: " + "; ".join(cons_all) + ".")
            reasons.append("README: if one figure improves and another gets worse, keep the "
                           "old one.")

    cmp = Comparison(a, b, winner, "", reasons, caveats, diffs, "")
    if winner in ("A", "B") and (_small(ta) or _small(tb)):
        nd = len(cmp.differing_frames())
        if nd <= 1:
            cmp.winner = winner = "inconclusive"
            reasons.append("The difference is %d frame of only %d: a different set of frames "
                           "could reverse it. Add frames before deciding." % (nd, ta.frames))
    if a.cand.stage >= 2 or b.cand.stage >= 2:
        caveats.append("From stage 2 up the pixel figures do not change; compare the blob "
                       "figures.")
    titles = {"A": "A is better", "B": "B is better", "tie": "A and B are equivalent",
              "inconclusive": "Cannot conclude that either is better"}
    tones = {"A": "ok", "B": "ok", "tie": "info", "inconclusive": "warn"}
    cmp.title, cmp.tone = titles[winner], tones[winner]
    if ta.false_b1 and tb.false_b1 and winner in ("A", "B"):
        cmp.title += ", but neither meets the blob 1 rule"
        cmp.tone = "warn"
    return cmp


def sum_totals(ts) -> Totals:
    out = Totals()
    for t in ts:
        for k in out.__dataclass_fields__:
            setattr(out, k, getattr(out, k) + getattr(t, k))
    return out


# ---- overlays for the frame viewer ---------------------------------------------

def boxes(f: FrameEval, ok_colour: str, bad_colour: str, gt_colour: str) -> list:
    """Boxes for the frame viewer: the labelled balloons (dotted) and the
    reported blobs, numbered as the camera reports them, with conf from 2 up."""
    out = [(c.x0, c.y0, c.x1 - c.x0 + 1, c.y1 - c.y0 + 1, gt_colour, "", "dot")
           for c in L.balloons(f.lab)]
    for k, b in enumerate(f.blobs):
        tag = "b%d" % (k + 1) + (" %.2f" % b.conf if b.conf >= 0 else "")
        out.append((b.x0, b.y0, b.x1 - b.x0 + 1, b.y1 - b.y0 + 1,
                    ok_colour if b.verdict == "balloon" else bad_colour, tag))
    return out
