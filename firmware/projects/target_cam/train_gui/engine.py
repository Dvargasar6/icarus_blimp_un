"""
Scoring a table on sessions, and the verdicts drawn from the scores.

The numbers are the ones eval_frames.py prints, from the same functions
(tcview.labels.score_frame through the shared library), kept per frame so the
GUI can show which frames fail and draw them. What is added here is the
judgement, written down once so the Verify and Compare pages cannot disagree:

Each evaluation is of a table at a stage: 1, the colour table alone, or 2,
the table and the blob classifier trained behind it (clf_model.bin in the
table's folder). At stage 2 the reported blobs are the ones the classifier
kept, so the blob figures change and the pixel figures do not.

Single table (README "Retraining the colour table", step 4): the table is
acceptable on the chosen folders only if every balloon is found (FN 0) and no
frame without a balloon gets a false blob 1. Blob 1 is what the blimp steers
to, so one false blob 1 fails the table however good the rest is. Folders the
table was trained on give optimistic figures, and the verdict says so.

Two tables (step 4, "if one figure improves and another gets worse, keep the
old table"): B is better than A when it is at least as good on every session
in every figure that matters (false blob 1, FN, FP, pixel F1) and strictly
better in at least one; the mirror image makes A better; everything equal is
a tie; anything mixed is inconclusive. A table with a false blob 1 on a
held-out empty frame is "not flyable", and the other one is preferred if it
has none, provided it does not also miss more balloons.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from tcview import detector as D
from tcview import labels as L

from . import paths
from .paths import Session, Table

# Pixel F1 differences smaller than this (in points) are noise, not a change:
# one frame's rim pixels move it by about that much.
PX_F1_TOL = 1.0


# ---- per-frame results ---------------------------------------------------------

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
    conf: float = -1.0      # the classifier's, at stage 2; -1 below


@dataclass
class FrameEval:
    session: str
    sf: L.SavedFrame
    lab: np.ndarray
    mask: np.ndarray
    blobs: list[Blob]
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
        if not s.n_balloons:
            return "b1 falso" if s.b1 == "false" else ("falso" if s.fp else "ok")
        if s.b1 == "false":
            return "b1 falso"
        if s.fn and s.tp == 0:
            return "perdido"
        if s.fn:
            return "parcial"
        if s.fp:
            return "falso"
        return "ok"

    def is_ok(self) -> bool:
        return self.outcome() == "ok"


def eval_frame(sf: L.SavedFrame, lab: np.ndarray, m: D.TcModels, session: str,
               stage: int = 1) -> FrameEval:
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


# ---- totals --------------------------------------------------------------------

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
    table: Table
    sessions: list[Session]
    frames: list[FrameEval]
    skipped: int = 0        # saved frames without labels
    stage: int = 1

    @property
    def label(self) -> str:
        """The table and stage, as the pages name what was evaluated."""
        return self.table.name + (" + clasificador" if self.stage >= 2 else "")

    def totals(self, session: str | None = None) -> Totals:
        t = Totals()
        for f in self.frames:
            if session is None or f.session == session:
                t.add(f)
        return t

    def by_session(self) -> dict[str, Totals]:
        return {s.key: self.totals(s.key) for s in self.sessions}

    def trained_on(self) -> list[str]:
        """Chosen folders the table (or, at stage 2, its classifier) was
        trained on: their figures are optimistic. By path where the .json
        records it, otherwise by name, which can also flag a same-named
        folder elsewhere."""
        out = []
        for s in self.sessions:
            if _trained_on(s, self.table.session_paths, self.table.sessions):
                # By name only (an older table): it may be another folder
                # of the same name, and the report should say it is a guess.
                out.append(s.key if self.table.session_paths else s.key + " (por nombre)")
            elif self.stage >= 2 and _trained_on(
                    s, [paths._resolve(p) for p in self.table.clf_meta.get("session_paths") or []],
                    self.table.clf_meta.get("sessions") or []):
                out.append(s.key + " (clasificador)")
        return out


def _trained_on(s: Session, used_paths: list[Path], used_names: list[str]) -> bool:
    if used_paths:
        return s.path.resolve() in used_paths
    return s.name in used_names or s.key in used_names


def models_for(table: Table, stage: int) -> D.TcModels:
    """The models a table offers at a stage. Stage 2 needs the classifier in
    the table's folder, trained behind this table (the C check refuses it
    otherwise, with the reason)."""
    if stage >= 2:
        if not table.has_clf:
            raise D.ModelError("la tabla %s no tiene clasificador (clf_model.bin)" % table.name)
        return D.models(table.bin, table.clf_bin)
    return D.models(table.bin)


def evaluate(table: Table, sessions: list[Session],
             progress: Optional[Callable[[int, int], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None, stage: int = 1) -> EvalResult:
    """Score `table` at `stage` (1 or 2) on every labelled frame of
    `sessions`. progress(done, total) is called per frame; cancelled() True
    stops early with what is done so far."""
    m = models_for(table, stage)
    stems = [(s, stem) for s in sessions for stem in L.list_frames(s.path)]
    res = EvalResult(table, list(sessions), [], stage=stage)
    for i, (s, stem) in enumerate(stems):
        if cancelled and cancelled():
            break
        sf = L.load_frame(stem)
        if not sf.label_path.exists():
            res.skipped += 1
            continue
        lab = L.load_labels(sf.label_path, sf.frame.w, sf.frame.h)
        res.frames.append(eval_frame(sf, lab, m, s.key, stage))
        if progress:
            progress(i + 1, len(stems))
    return res


# ---- verdicts ------------------------------------------------------------------

@dataclass
class Verdict:
    ok: bool
    title: str
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    tone: str = "ok"


def judge(res: EvalResult) -> Verdict:
    """README step 4 on one table: every balloon found, no false blob 1."""
    t = res.totals()
    reasons, ok = [], True
    if t.frames == 0:
        return Verdict(False, "Sin fotogramas etiquetados", ["Ninguna carpeta elegida tiene "
                       "fotogramas con .labels.png."], tone="err")
    if t.false_b1:
        ok = False
        reasons.append("%d de %d fotogramas sin globo tienen un blob 1 falso: el dirigible "
                       "iría hacia un falso objetivo. Esta tabla no se debe volar." %
                       (t.false_b1, t.empty))
    if t.fn:
        ok = False
        reasons.append("%d de %d globos no fueron encontrados (FN)." % (t.fn, t.balloons))
    if t.fp:
        reasons.append("%d blobs falsos (FP) en total%s." % (
            t.fp, "" if t.false_b1 else ", ninguno como blob 1"))
    if ok:
        reasons.insert(0, "Todos los globos encontrados (%d/%d) y ningún blob 1 falso en %d "
                       "fotogramas sin globo." % (t.tp, t.balloons, t.empty))
    caveats = []
    trained = res.trained_on()
    if trained:
        caveats.append("La tabla se entrenó con %s: sus cifras ahí son optimistas, sirven "
                       "para ver fallos graves, no como puntuación." % ", ".join(trained))
    if res.skipped:
        caveats.append("%d fotogramas guardados sin etiquetas quedaron fuera." % res.skipped)
    if ok:
        title = "Cumple los criterios del README en estas carpetas"
        tone = "ok" if not trained else "info"
    else:
        title = "No cumple los criterios del README"
        tone = "err" if t.false_b1 else "warn"
    return Verdict(ok, title, reasons, caveats, tone)


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


def _edges(a: Totals, b: Totals) -> tuple[list[str], list[str]]:
    """What B does better than A (pros) and worse (cons), as short phrases."""
    pros, cons = [], []

    def lower(name, va, vb):
        if vb < va:
            pros.append("%s %d → %d" % (name, va, vb))
        elif vb > va:
            cons.append("%s %d → %d" % (name, va, vb))

    lower("blob 1 falso", a.false_b1, b.false_b1)
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
class Comparison:
    a: EvalResult
    b: EvalResult
    winner: str             # "A" | "B" | "tie" | "inconclusive"
    title: str
    reasons: list[str]
    caveats: list[str]
    sessions: list[SessionDiff]
    tone: str

    def differing_frames(self, px_tol: float = 0.05) -> list[tuple[FrameEval, FrameEval]]:
        """Frames the two tables treat differently, paired: a different blob
        outcome, or pixel precision or recall apart by more than px_tol."""
        bya = {(f.session, f.name): f for f in self.a.frames}
        out = []
        for fb in self.b.frames:
            fa = bya.get((fb.session, fb.name))
            if fa is None:
                continue
            sa, sb = fa.score, fb.score
            blob_diff = (fa.outcome() != fb.outcome() or sa.fn != sb.fn or sa.fp != sb.fp
                         or sa.b1 != sb.b1)
            px_diff = any(abs(_nz(L.ratio(x, x + y)) - _nz(L.ratio(u, u + v))) > px_tol
                          for (x, y), (u, v) in (((sa.px_tp, sa.px_fp), (sb.px_tp, sb.px_fp)),
                                                 ((sa.px_tp, sa.px_fn), (sb.px_tp, sb.px_fn))))
            if blob_diff or px_diff:
                out.append((fa, fb))
        return out


def _nz(x: float) -> float:
    return 0.0 if math.isnan(x) else x


def compare(a: EvalResult, b: EvalResult) -> Comparison:
    names = [s.key for s in a.sessions if s.key in {x.key for x in b.sessions}]
    diffs = [SessionDiff(n, a.totals(n), b.totals(n)) for n in names]
    ta, tb = a.totals(), b.totals()
    reasons, caveats = [], []

    # The hard rule first: a false blob 1 on a frame without a balloon.
    if ta.false_b1 and not tb.false_b1 and tb.fn <= ta.fn:
        winner = "B"
        reasons.append("A pone un blob 1 falso en %d fotograma(s) sin globo y B en ninguno, "
                       "sin perder más globos (FN %d → %d)." % (ta.false_b1, ta.fn, tb.fn))
    elif tb.false_b1 and not ta.false_b1 and ta.fn <= tb.fn:
        winner = "A"
        reasons.append("B pone un blob 1 falso en %d fotograma(s) sin globo y A en ninguno, "
                       "sin perder más globos (FN %d → %d)." % (tb.false_b1, tb.fn, ta.fn))
    else:
        if ta.false_b1 and tb.false_b1:
            reasons.append("Ambas tablas ponen un blob 1 falso en algún fotograma sin globo "
                           "(A %d, B %d): ninguna se debe volar tal cual." %
                           (ta.false_b1, tb.false_b1))
        pros_all, cons_all = [], []
        for d in diffs:
            pros, cons = _edges(d.a, d.b)
            pros_all += ["%s: %s" % (d.session, p) for p in pros]
            cons_all += ["%s: %s" % (d.session, c) for c in cons]
        if pros_all and not cons_all:
            winner = "B"
            reasons.append("B iguala o mejora a A en todas las sesiones y cifras, y mejora en: "
                           + "; ".join(pros_all) + ".")
        elif cons_all and not pros_all:
            winner = "A"
            reasons.append("A iguala o mejora a B en todas las sesiones y cifras, y mejora en: "
                           + "; ".join(cons_all) + ".")
        elif not pros_all and not cons_all:
            winner = "tie"
            reasons.append("Las dos tablas dan las mismas cifras (px F1 dentro de %.0f punto) "
                           "en todas las sesiones." % PX_F1_TOL)
        else:
            winner = "inconclusive"
            reasons.append("B mejora en: " + "; ".join(pros_all) + ".")
            reasons.append("B empeora en: " + "; ".join(cons_all) + ".")
            reasons.append("El README dice: si una cifra mejora y otra empeora, se conserva la "
                           "tabla anterior.")

    for tag, r in (("A", a), ("B", b)):
        tr = r.trained_on()
        if tr:
            caveats.append("La tabla %s se entrenó con %s: ahí sus cifras son optimistas."
                           % (tag, ", ".join(tr)))
    only_a = {s.key for s in a.sessions} - set(names)
    only_b = {s.key for s in b.sessions} - set(names)
    if only_a or only_b:
        caveats.append("Solo se comparan las sesiones evaluadas con ambas tablas.")
    if not names:
        return Comparison(a, b, "inconclusive", "Sin sesiones en común", [], caveats, [], "err")

    titles = {"A": "La tabla A es mejor", "B": "La tabla B es mejor",
              "tie": "Las tablas son equivalentes",
              "inconclusive": "No se puede concluir que una sea mejor"}
    tones = {"A": "ok", "B": "ok", "tie": "info", "inconclusive": "warn"}
    title, tone = titles[winner], tones[winner]
    if ta.false_b1 and tb.false_b1 and winner in ("A", "B"):
        # Better of two that both fail the hard rule: say so in the headline.
        title += ", pero ninguna cumple el criterio del blob 1"
        tone = "warn"
    return Comparison(a, b, winner, title, reasons, caveats, diffs, tone)


# ---- overlays for the frame viewer -------------------------------------------

def mask_overlay(mask: np.ndarray, rgba=(242, 193, 78, 110)) -> np.ndarray:
    """Matching pixels tinted; everything else transparent."""
    h, w = mask.shape
    out = np.zeros((h, w, 4), dtype=np.uint8)
    out[mask.astype(bool)] = rgba
    return out


def rgb(f: FrameEval) -> np.ndarray:
    return f.sf.frame.rgb()


def boxes(f: FrameEval, ok_colour: str, bad_colour: str, gt_colour: str) -> list:
    """PixelImageView boxes: the labelled balloons (dotted) and the reported
    blobs, numbered as the camera reports them."""
    out = [(c.x0, c.y0, c.x1 - c.x0 + 1, c.y1 - c.y0 + 1, gt_colour, "", "dot")
           for c in L.balloons(f.lab)]
    for k, b in enumerate(f.blobs):
        tag = "b%d" % (k + 1) + (" %.2f" % b.conf if b.conf >= 0 else "")
        out.append((b.x0, b.y0, b.x1 - b.x0 + 1, b.y1 - b.y0 + 1,
                    ok_colour if b.verdict == "balloon" else bad_colour, tag))
    return out
