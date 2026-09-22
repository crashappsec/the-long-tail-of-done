#!/usr/bin/env python3
"""Declared metrics: a restricted expression language over build records.

Every derived number in this tool is a formula in a registry rather than a
hard-coded column, so a team can supply its own definition of `debt` without
patching the tool. That makes formulas *data*, and data from a teammate must
not become code -- hence a restricted AST walker rather than `eval`.

What the language is:

    names        raw build columns and per-build derived names
    numbers      int and float literals
    operators    + - * / ** and unary minus, plus comparisons inside predicates
    functions    sum median mean p90 p10 min max count share abs sqrt log
                 minmax z clip

What it is not: no imports, no attribute access, no subscripts, no
comprehensions, no lambdas, no calls to anything outside the allowlist, and no
names outside the schema. Every rejection names the offending node type.

Two evaluation modes, because a cell-level metric is a reduction over builds:

    SCALAR mode   outside an aggregate. `sum(cost_usd) / count()` lives here.
    VECTOR mode   inside an aggregate's argument. `loc_added + loc_removed`
                  evaluates elementwise, one value per build.

A bare column name in SCALAR mode is an error: `cost_usd` alone does not say
which of the cell's builds it means.

Two passes, because `minmax` and `z` are cell-level normalisations that need
every cell's value before any cell's value is final:

    pass 1   evaluate(builds)                -> deferred inner values
    pass 2   evaluate(builds, norm=params)   -> the normalised result

Null policy, and it is the same rule as the input schema: nullable means "we
could not observe it", never zero. Aggregates skip nulls and report how many
they skipped; an aggregate over nothing but nulls is None, not 0.0.
"""

from __future__ import annotations

import ast
import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Schema the language may refer to
# ---------------------------------------------------------------------------

# Raw columns, straight off builds.csv.
RAW_NUMERIC = {
    "usage",
    "loc_added",
    "loc_removed",
    "bugs",
    "cost_usd",
}
RAW_TIMESTAMP = {"first_commit_ts", "ts", "deployed_at", "served_at"}
RAW_STRING = {"build_id", "group", "question", "carried_into"}

# Per-build names the classifier adds before any formula is evaluated.
DERIVED_NUMERIC = {"lead_time_days", "loc_total", "age_days"}
DERIVED_STRING = {"impact", "lead_time_source"}
DERIVED_BOOL = {"deployed", "served", "gate_unavailable"}

IMPACT_CLASSES = (
    "impactful",
    "low_impact",
    "liability",
    "carried",
    "sunk",
    "unresolved",
)
QUESTION_CLASSES = ("features", "correctness", "performance", "economics")

KNOWN_NAMES = (
    RAW_NUMERIC
    | RAW_TIMESTAMP
    | RAW_STRING
    | DERIVED_NUMERIC
    | DERIVED_STRING
    | DERIVED_BOOL
)

# Reductions over the builds in a cell. Argument is evaluated in VECTOR mode.
AGGREGATES = {"sum", "median", "mean", "p90", "p10", "min", "max"}
# Cell-level normalisations, resolved in pass 2 once every cell is known.
NORMALISERS = {"minmax", "z"}
# Everything else callable.
SCALAR_FUNCS = {"abs", "sqrt", "log", "clip"}
COUNTERS = {"count", "share"}

ALLOWED_FUNCS = AGGREGATES | NORMALISERS | SCALAR_FUNCS | COUNTERS

ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Name,
    ast.Constant,
    ast.Compare,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.Not,
)


class FormulaError(ValueError):
    """A formula that cannot be trusted, with the reason in the message."""


# ---------------------------------------------------------------------------
# Percentiles and medians, on the stdlib
# ---------------------------------------------------------------------------


def _percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile, q in [0, 1]. None over an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(ordered[lo])
    frac = pos - lo
    return float(ordered[lo] * (1 - frac) + ordered[hi] * frac)


def median(values: list[float]) -> float | None:
    return _percentile(values, 0.5)


# ---------------------------------------------------------------------------
# Dependencies, for the structural overlap report
# ---------------------------------------------------------------------------


@dataclass
class Deps:
    """What a formula reads. Computed from the tree, before any data exists."""

    columns: set[str] = field(default_factory=set)
    mix_parts: set[str] = field(default_factory=set)
    question_parts: set[str] = field(default_factory=set)
    functions: set[str] = field(default_factory=set)

    def as_json(self) -> dict:
        return {
            "columns": sorted(self.columns),
            "mix_parts": sorted(self.mix_parts),
            "question_parts": sorted(self.question_parts),
            "functions": sorted(self.functions),
        }


# Raw columns the impact gate itself consumes. An objective that reads one of
# these shares an input with every part of the impact mix -- softer than
# consuming a mix part outright, and reported separately so the hard finding
# does not drown in it.
GATE_INPUTS = {"deployed_at", "usage", "carried_into", "ts", "impact", "deployed"}


# ---------------------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    value: float | None
    deferred: dict[int, float | None]
    nulls_skipped: int
    notes: list[str]


class Formula:
    """One parsed, validated expression, reusable across cells."""

    def __init__(self, name: str, expression: str) -> None:
        self.name = name
        self.expression = expression
        if len(expression) > 4096:
            raise FormulaError(f"{name}: expression exceeds 4096 characters")
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise FormulaError(f"{name}: cannot parse: {exc.msg}") from exc
        self.tree = tree
        self._validate(tree)
        # Stable indices for the normaliser nodes, assigned in source order, so
        # pass 1 and pass 2 agree on which deferred value is which.
        self._norm_nodes: list[ast.Call] = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in NORMALISERS
        ]
        self._norm_nodes.sort(key=lambda n: (n.lineno, n.col_offset))
        self._norm_index = {id(node): i for i, node in enumerate(self._norm_nodes)}
        self.deps = self._collect_deps(tree)

    # -- validation ---------------------------------------------------------

    def _validate(self, tree: ast.Expression) -> None:
        nodes = list(ast.walk(tree))
        if len(nodes) > 512:
            raise FormulaError(f"{self.name}: expression exceeds 512 AST nodes")
        pending = [(tree, 0)]
        while pending:
            node, depth = pending.pop()
            if depth > 64:
                raise FormulaError(f"{self.name}: expression nesting exceeds 64 levels")
            pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
        for node in ast.walk(tree):
            if not isinstance(node, ALLOWED_NODES):
                raise FormulaError(
                    f"{self.name}: {type(node).__name__} is not allowed in a formula"
                )
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name):
                    raise FormulaError(
                        f"{self.name}: only plain function names may be called"
                    )
                if node.func.id not in ALLOWED_FUNCS:
                    raise FormulaError(
                        f"{self.name}: {node.func.id}() is not in the allowlist "
                        f"({', '.join(sorted(ALLOWED_FUNCS))})"
                    )
                if node.keywords:
                    raise FormulaError(f"{self.name}: keyword arguments are not allowed")
            if isinstance(node, ast.Constant) and not isinstance(
                node.value, (int, float, str, bool)
            ):
                raise FormulaError(
                    f"{self.name}: {type(node.value).__name__} literals are not allowed"
                )
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                try:
                    finite = math.isfinite(float(node.value))
                except OverflowError:
                    finite = False
                if not finite:
                    raise FormulaError(f"{self.name}: numeric literals must be finite")
        self._validate_names(tree.body, vector=False)
        if self._value_type(tree.body) != "number":
            raise FormulaError(f"{self.name}: a metric must produce a number")

    def _value_type(self, node: ast.AST) -> str:
        if isinstance(node, ast.Constant):
            return "string" if isinstance(node.value, str) else "bool" if isinstance(node.value, bool) else "number"
        if isinstance(node, ast.Name):
            if node.id in RAW_NUMERIC | DERIVED_NUMERIC:
                return "number"
            return "bool" if node.id in DERIVED_BOOL else "string"
        if isinstance(node, ast.Call):
            fn = node.func.id
            if fn in COUNTERS:
                if node.args and not isinstance(node.args[0], ast.Name):
                    if self._value_type(node.args[0]) != "bool":
                        raise FormulaError(f"{self.name}: {fn} requires a predicate")
                return "number"
            children = node.args
        elif isinstance(node, ast.Compare):
            if len(node.ops) != 1 or self._value_type(node.left) != self._value_type(node.comparators[0]):
                raise FormulaError(f"{self.name}: comparison requires two compatible values")
            return "bool"
        elif isinstance(node, ast.BoolOp) or (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)):
            children = node.values if isinstance(node, ast.BoolOp) else [node.operand]
            if any(self._value_type(child) != "bool" for child in children):
                raise FormulaError(f"{self.name}: boolean operators require predicates")
            return "bool"
        elif isinstance(node, ast.BinOp):
            children = [node.left, node.right]
        else:
            children = [node.operand]
        if any(self._value_type(child) != "number" for child in children):
            raise FormulaError(f"{self.name}: arithmetic requires numeric values")
        return "number"

    def _validate_names(self, node: ast.AST, vector: bool) -> None:
        """Names must be in the schema, and only mean a build value in VECTOR mode."""
        if isinstance(node, ast.Name):
            if node.id in KNOWN_NAMES:
                if not vector:
                    raise FormulaError(
                        f"{self.name}: '{node.id}' is a per-build column and needs an "
                        f"aggregate around it, e.g. sum({node.id}) or median({node.id})"
                    )
                return
            if node.id in IMPACT_CLASSES or node.id in QUESTION_CLASSES:
                return
            raise FormulaError(
                f"{self.name}: '{node.id}' is not a column in the schema"
            )
        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name)  # _validate ran first
            fn = node.func.id
            if fn in AGGREGATES:
                if vector:
                    raise FormulaError(f"{self.name}: nested aggregates are not allowed")
                self._expect_args(node, 1, fn)
                self._validate_names(node.args[0], vector=True)
                return
            if fn in COUNTERS:
                if vector:
                    raise FormulaError(f"{self.name}: counters cannot nest inside aggregates")
                if fn == "count" and not node.args:
                    return
                self._expect_args(node, 1, fn)
                self._validate_predicate(node.args[0], fn)
                return
            if fn in NORMALISERS:
                if vector or any(
                    isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                    and child.func.id in NORMALISERS
                    for arg in node.args for child in ast.walk(arg)
                ):
                    raise FormulaError(f"{self.name}: normalisers require a scalar and cannot nest")
                self._expect_args(node, 1, fn)
                self._validate_names(node.args[0], vector=vector)
                return
            if fn == "clip":
                self._expect_args(node, 3, fn)
                self._validate_names(node.args[0], vector=vector)
                for arg in node.args[1:]:
                    self._validate_names(arg, vector=False)
                return
            self._expect_args(node, 1, fn)
            for arg in node.args:
                self._validate_names(arg, vector=vector)
            return
        for child in ast.iter_child_nodes(node):
            self._validate_names(child, vector)

    def _expect_args(self, node: ast.Call, n: int, fn: str) -> None:
        if len(node.args) != n:
            raise FormulaError(
                f"{self.name}: {fn}() takes {n} argument(s), got {len(node.args)}"
            )

    def _validate_predicate(self, node: ast.AST, fn: str) -> None:
        """count()/share() take a comparison, or a bare class name as sugar."""
        if isinstance(node, ast.Name):
            if node.id in IMPACT_CLASSES or node.id in QUESTION_CLASSES:
                return
            raise FormulaError(
                f"{self.name}: {fn}({node.id}) -- a bare name must be an impact class "
                f"({', '.join(IMPACT_CLASSES)}) or a question class "
                f"({', '.join(QUESTION_CLASSES)})"
            )
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                raise FormulaError(f"{self.name}: chained comparisons are not allowed")
            self._validate_names(node.left, vector=True)
            right = node.comparators[0]
            if isinstance(right, ast.Name) and right.id not in KNOWN_NAMES:
                return  # bare identifier on the right is a string literal
            self._validate_names(right, vector=True)
            return
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                self._validate_predicate(value, fn)
            return
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            self._validate_predicate(node.operand, fn)
            return
        raise FormulaError(
            f"{self.name}: {fn}() takes a comparison or a class name, "
            f"not {type(node).__name__}"
        )

    # -- dependencies -------------------------------------------------------

    def _collect_deps(self, tree: ast.Expression) -> Deps:
        deps = Deps()

        def walk_predicate(node: ast.AST) -> None:
            if isinstance(node, ast.Name):
                if node.id in IMPACT_CLASSES:
                    deps.mix_parts.add(node.id)
                    deps.columns.add("impact")
                elif node.id in QUESTION_CLASSES:
                    deps.question_parts.add(node.id)
                    deps.columns.add("question")
                elif node.id in KNOWN_NAMES:
                    deps.columns.add(node.id)
                return
            if isinstance(node, ast.Compare):
                left = node.left
                right = node.comparators[0]
                if isinstance(left, ast.Name) and left.id in KNOWN_NAMES:
                    deps.columns.add(left.id)
                    if left.id in ("impact", "question") and isinstance(
                        right, ast.Name
                    ):
                        if right.id in IMPACT_CLASSES:
                            deps.mix_parts.add(right.id)
                        elif right.id in QUESTION_CLASSES:
                            deps.question_parts.add(right.id)
                    elif left.id in ("impact", "question") and isinstance(
                        right, ast.Constant
                    ):
                        if right.value in IMPACT_CLASSES:
                            deps.mix_parts.add(str(right.value))
                        elif right.value in QUESTION_CLASSES:
                            deps.question_parts.add(str(right.value))
                for child in (left, right):
                    if not isinstance(child, ast.Name):
                        walk_predicate(child)
                return
            for child in ast.iter_child_nodes(node):
                walk_predicate(child)

        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in KNOWN_NAMES:
                deps.columns.add(node.id)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                deps.functions.add(node.func.id)
                if node.func.id in COUNTERS and node.args:
                    walk_predicate(node.args[0])
        # `share(x)` with no predicate arg is impossible, and a bare `count()`
        # depends on nothing but the cell's build list.
        return deps

    # -- evaluation ---------------------------------------------------------

    def evaluate(
        self, builds: list[dict], norm: dict[int, dict] | None = None
    ) -> EvalResult:
        """Evaluate for one cell. Pass 1 with norm=None, pass 2 with the params."""
        ctx = _Ctx(builds=builds, norm=norm, formula=self)
        try:
            value = self._eval(self.tree.body, ctx, vector=False)
        except (OverflowError, ValueError) as exc:
            raise FormulaError(f"{self.name}: arithmetic outside the supported numeric range") from exc
        if isinstance(value, list):  # unreachable: SCALAR mode is enforced
            raise FormulaError(f"{self.name}: expression is per-build, not per-cell")
        if value is not None and not math.isfinite(float(value)):
            ctx.note(f"{self.name}: non-finite result -> null")
            value = None
        return EvalResult(
            value=None if value is None else float(value),
            deferred=ctx.deferred,
            nulls_skipped=ctx.nulls_skipped,
            notes=ctx.notes,
        )

    def _eval(self, node: ast.AST, ctx: "_Ctx", vector: bool):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return node.value
            return node.value if isinstance(node.value, bool) else float(node.value)
        if isinstance(node, ast.Name):
            if vector and node.id in KNOWN_NAMES:
                return [b.get(node.id) for b in ctx.builds]
            # A bare identifier that is not a column is a string literal, which
            # is how `impact == liability` reads without quotes.
            return node.id
        if isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand, ctx, vector)
            if isinstance(node.op, ast.Not):
                return _lift1(lambda v: not v, operand)
            if isinstance(node.op, ast.USub):
                return _lift1(lambda v: -v, operand)
            return operand
        if isinstance(node, ast.BinOp):
            left = self._eval(node.left, ctx, vector)
            right = self._eval(node.right, ctx, vector)
            return self._binop(node.op, left, right, ctx)
        if isinstance(node, ast.BoolOp):
            values = [self._eval(v, ctx, vector) for v in node.values]
            op = (lambda a, b: bool(a) and bool(b)) if isinstance(node.op, ast.And) else (
                lambda a, b: bool(a) or bool(b)
            )
            out = values[0]
            for nxt in values[1:]:
                out = _lift2(op, out, nxt)
            return out
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, ctx, vector)
            right = self._eval(node.comparators[0], ctx, vector)
            return _lift2(_comparator(node.ops[0]), left, right)
        if isinstance(node, ast.Call):
            return self._call(node, ctx, vector)
        raise FormulaError(f"{self.name}: cannot evaluate {type(node).__name__}")

    def _binop(self, op: ast.AST, left, right, ctx: "_Ctx"):
        if isinstance(op, ast.Add):
            return _lift2(lambda a, b: a + b, left, right)
        if isinstance(op, ast.Sub):
            return _lift2(lambda a, b: a - b, left, right)
        if isinstance(op, ast.Mult):
            return _lift2(lambda a, b: a * b, left, right)
        if isinstance(op, ast.Pow):
            def power(a, b):
                try:
                    value = math.pow(float(a), float(b))
                    return value if math.isfinite(value) else None
                except (OverflowError, ValueError):
                    ctx.note(f"{self.name}: power outside finite real numbers -> null")
                    return None
            return _lift2(power, left, right)
        if isinstance(op, ast.Div):
            def div(a, b):
                if b == 0:
                    ctx.note(f"{self.name}: division by zero -> null")
                    return None
                return a / b

            return _lift2(div, left, right)
        raise FormulaError(f"{self.name}: operator {type(op).__name__} is not allowed")

    def _call(self, node: ast.Call, ctx: "_Ctx", vector: bool):
        assert isinstance(node.func, ast.Name)  # _validate ran first
        fn = node.func.id
        if fn in AGGREGATES:
            values = self._eval(node.args[0], ctx, vector=True)
            if not isinstance(values, list):
                values = [values] * len(ctx.builds)
            clean = [
                float(v)
                for v in values
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            ]
            ctx.nulls_skipped += sum(1 for v in values if v is None)
            if not clean:
                return None
            if fn == "sum":
                return math.fsum(clean)
            if fn == "mean":
                return math.fsum(clean) / len(clean)
            if fn == "median":
                return median(clean)
            if fn == "p90":
                return _percentile(clean, 0.9)
            if fn == "p10":
                return _percentile(clean, 0.1)
            if fn == "min":
                return min(clean)
            if fn == "max":
                return max(clean)
        if fn == "count":
            if not node.args:
                return float(len(ctx.builds))
            flags = self._eval(node.args[0], ctx, vector=True)
            flags = self._as_flags(node.args[0], flags, ctx)
            return float(sum(1 for f in flags if f))
        if fn == "share":
            flags = self._eval(node.args[0], ctx, vector=True)
            flags = self._as_flags(node.args[0], flags, ctx)
            if not ctx.builds:
                return None
            return float(sum(1 for f in flags if f)) / len(ctx.builds)
        if fn in NORMALISERS:
            inner = self._eval(node.args[0], ctx, vector)
            if inner is not None and not math.isfinite(float(inner)):
                ctx.note(f"{self.name}: non-finite normaliser input -> null")
                inner = None
            index = self._norm_index[id(node)]
            if ctx.norm is None:
                ctx.deferred[index] = None if inner is None else float(inner)
                return 0.0  # placeholder; pass 2 replaces it
            params = ctx.norm.get(index) or {}
            return _apply_norm(fn, inner, params, ctx, self.name)
        if fn == "abs":
            return _lift1(abs, self._eval(node.args[0], ctx, vector))
        if fn == "sqrt":
            return _lift1(
                lambda v: math.sqrt(v) if v >= 0 else None,
                self._eval(node.args[0], ctx, vector),
            )
        if fn == "log":
            return _lift1(
                lambda v: math.log(v) if v > 0 else None,
                self._eval(node.args[0], ctx, vector),
            )
        if fn == "clip":
            value = self._eval(node.args[0], ctx, vector)
            lo = self._eval(node.args[1], ctx, vector)
            hi = self._eval(node.args[2], ctx, vector)
            if lo is None or hi is None:
                return None
            return _lift1(lambda v: max(lo, min(hi, v)), value)
        raise FormulaError(f"{self.name}: {fn}() reached evaluation unhandled")

    def _as_flags(self, arg: ast.AST, value, ctx: "_Ctx") -> list[bool]:
        """Resolve a predicate to one boolean per build, expanding class sugar."""
        if isinstance(arg, ast.Name) and arg.id in IMPACT_CLASSES:
            return [b.get("impact") == arg.id for b in ctx.builds]
        if isinstance(arg, ast.Name) and arg.id in QUESTION_CLASSES:
            return [b.get("question") == arg.id for b in ctx.builds]
        if isinstance(value, list):
            return [bool(v) for v in value]
        return [bool(value)] * len(ctx.builds)

    # -- normalisation params ----------------------------------------------

    @property
    def norm_node_count(self) -> int:
        return len(self._norm_nodes)

    def norm_kind(self, index: int) -> str:
        func = self._norm_nodes[index].func
        assert isinstance(func, ast.Name)
        return func.id


@dataclass
class _Ctx:
    builds: list[dict]
    norm: dict[int, dict] | None
    formula: Formula
    deferred: dict[int, float | None] = field(default_factory=dict)
    nulls_skipped: int = 0
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)


def _comparator(op: ast.AST):
    if isinstance(op, ast.Eq):
        return lambda a, b: a == b
    if isinstance(op, ast.NotEq):
        return lambda a, b: a != b
    if isinstance(op, ast.Lt):
        return lambda a, b: a < b
    if isinstance(op, ast.LtE):
        return lambda a, b: a <= b
    if isinstance(op, ast.Gt):
        return lambda a, b: a > b
    if isinstance(op, ast.GtE):
        return lambda a, b: a >= b
    raise FormulaError(f"comparison {type(op).__name__} is not allowed")


def _lift1(fn, value):
    """Apply fn elementwise over a vector, or once over a scalar. None passes through."""
    if isinstance(value, list):
        return [None if v is None else fn(v) for v in value]
    return None if value is None else fn(value)


def _lift2(fn, left, right):
    if isinstance(left, list) or isinstance(right, list):
        n = len(left) if isinstance(left, list) else len(right)
        lv = left if isinstance(left, list) else [left] * n
        rv = right if isinstance(right, list) else [right] * n
        out = []
        for a, b in zip(lv, rv):
            if a is None or b is None:
                out.append(None)
            else:
                try:
                    out.append(fn(a, b))
                except TypeError:
                    out.append(None)
        return out
    if left is None or right is None:
        return None
    try:
        return fn(left, right)
    except TypeError:
        return None


def _apply_norm(fn: str, value, params: dict, ctx: _Ctx, name: str):
    if value is None:
        return None
    if fn == "minmax":
        lo, hi = params.get("lo"), params.get("hi")
        if lo is None or hi is None:
            return None
        if hi == lo:
            # Degenerate across cells: every cell identical. 0.5 rather than a
            # division by zero, and stage 2 will flag it as redundant.
            ctx.note(f"{name}: minmax over a constant objective -> 0.5")
            return 0.5 + (value - lo) / max(1.0, abs(lo))
        return (value - lo) / (hi - lo)
    if fn == "z":
        mu, sd = params.get("mean"), params.get("sd")
        if mu is None or sd is None:
            return None
        if sd == 0:
            ctx.note(f"{name}: z over a constant objective -> 0.0")
            return (value - mu) / max(1.0, abs(mu))
        return (value - mu) / sd
    raise FormulaError(f"{name}: unknown normaliser {fn}")


def norm_params_from_deferred(
    formula: Formula, per_cell: list[dict[int, float | None]]
) -> dict[int, dict]:
    """Collapse pass-1 deferred values into per-node normalisation parameters."""
    params: dict[int, dict] = {}
    for index in range(formula.norm_node_count):
        values = [
            float(cell[index])
            for cell in per_cell
            if cell.get(index) is not None
        ]
        kind = formula.norm_kind(index)
        if not values:
            params[index] = {}
            continue
        if kind == "minmax":
            params[index] = {"lo": min(values), "hi": max(values)}
        else:
            mu = math.fsum(values) / len(values)
            var = math.fsum((v - mu) ** 2 for v in values) / max(1, len(values) - 1)
            params[index] = {"mean": mu, "sd": math.sqrt(var)}
    return params


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


@dataclass
class Metric:
    name: str
    role: str  # objective | descriptor
    direction: str  # min | max, required for objectives
    expression: str
    version: int = 1
    notes: str = ""
    formula: Formula | None = None

    def compile(self) -> "Metric":
        if self.role not in ("objective", "descriptor"):
            raise FormulaError(
                f"{self.name}: role must be objective or descriptor, not '{self.role}'"
            )
        if self.role == "objective" and self.direction not in ("min", "max"):
            raise FormulaError(
                f"{self.name}: an objective needs direction min or max"
            )
        self.formula = Formula(self.name, self.expression)
        return self

    @property
    def sign(self) -> float:
        """+1 if the metric already minimises, -1 if it must be flipped."""
        return 1.0 if self.direction != "max" else -1.0

    def as_json(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "direction": self.direction,
            "expression": self.expression,
            "version": self.version,
            "notes": self.notes,
            "deps": self.formula.deps.as_json() if self.formula else None,
        }


# Shipped defaults. Seeded into a fresh store; see the skill's SCHEMA.md section 6.
# `debt_composite` is the supplied heuristic: term one is bugs per KLOC, since
# LOC-per-bug is inverted on a minimise axis, and both terms are normalised to
# [0,1] before the weighted sum or raw defect density swamps a share.
DEFAULT_METRICS: list[Metric] = [
    Metric("cost", "objective", "min", "mean(cost_usd)", 2,
           "mean fully-loaded cost per build"),
    Metric("cost_kloc", "descriptor", "min",
           "sum(cost_usd) / (sum(loc_total) / 1000)", 1,
           "cost per KLOC changed"),
    Metric("cost_impact", "descriptor", "min",
           "sum(cost_usd) / count(impact == impactful)", 1,
           "cost per impactful build -- OVERLAPS the impact mix by construction"),
    Metric("lead_time", "objective", "min", "median(lead_time_days)", 1,
           "median first-commit to served/deployed, in days"),
    Metric("lead_time_p90", "descriptor", "min", "p90(lead_time_days)", 1,
           "P90 lead time, drawn as a whisker"),
    Metric("debt_defect", "objective", "min",
           "minmax(sum(bugs) / (sum(loc_total) / 1000))", 1,
           "defect density, bugs per KLOC, min-max normalised across cells"),
    Metric("debt_composite", "objective", "min",
           "0.5 * minmax(sum(bugs) / (sum(loc_total) / 1000)) "
           "+ 0.5 * (share(liability) + share(sunk))", 1,
           "weights 0.5/0.5: half defect density, half wasted-build share. "
           "OVERLAPS share(liability) and share(sunk)"),
    Metric("throughput", "descriptor", "max", "count()", 1,
           "builds in the cell"),
]


def default_metrics() -> list[Metric]:
    return [
        Metric(m.name, m.role, m.direction, m.expression, m.version, m.notes).compile()
        for m in DEFAULT_METRICS
    ]


def read_metrics_csv(path: Path) -> list[Metric]:
    metrics: list[Metric] = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not row.get("name") or row["name"].startswith("#"):
                continue
            metrics.append(
                Metric(
                    name=row["name"].strip(),
                    role=(row.get("role") or "descriptor").strip(),
                    direction=(row.get("direction") or "min").strip(),
                    expression=row["expression"],
                    version=int(row.get("version") or 1),
                    notes=(row.get("notes") or "").strip(),
                ).compile()
            )
    return metrics


# ---------------------------------------------------------------------------
# Structural overlap: computed from the definitions, before any data is read
# ---------------------------------------------------------------------------


@dataclass
class Overlap:
    metric: str
    kind: str  # mix_part | question_part | gate_input
    shared: list[str]

    def line(self) -> str:
        if self.kind == "gate_input":
            return (
                f"SHARED_INPUT: {self.metric} <- {', '.join(self.shared)} "
                f"(also read by the impact gate)"
            )
        return f"OVERLAP: {self.metric} <- {', '.join(self.shared)}"


def overlap_report(objectives: list[Metric]) -> list[Overlap]:
    """Objectives that are not independent evidence from the mixes they sit beside.

    Two severities, deliberately. Consuming `share(liability)` in an objective
    means the objective axis and a glyph arm are literally the same
    measurement -- that is OVERLAP, and it greys the spoke. Merely reading a
    column the gate also reads (`deployed_at` for lead time, say) is a weaker
    relationship worth printing and not worth greying, or every timing metric
    in the tool would be flagged and the real finding would drown.
    """
    found: list[Overlap] = []
    for metric in objectives:
        if metric.formula is None:
            metric.compile()
        assert metric.formula is not None
        deps = metric.formula.deps
        if deps.mix_parts:
            found.append(
                Overlap(
                    metric.name,
                    "mix_part",
                    [f"share({p})" for p in sorted(deps.mix_parts)],
                )
            )
        if deps.question_parts:
            found.append(
                Overlap(
                    metric.name,
                    "question_part",
                    [f"share({p})" for p in sorted(deps.question_parts)],
                )
            )
        gate_shared = sorted((deps.columns & GATE_INPUTS) - {"impact"})
        if gate_shared and not deps.mix_parts:
            found.append(Overlap(metric.name, "gate_input", gate_shared))
    return found


def greyed_spokes(overlaps: list[Overlap]) -> dict[str, list[str]]:
    """Mix parts to grey in every glyph, mapped to the objectives that consume them."""
    greyed: dict[str, list[str]] = {}
    for overlap in overlaps:
        if overlap.kind not in ("mix_part", "question_part"):
            continue
        for shared in overlap.shared:
            part = shared[len("share(") : -1]
            greyed.setdefault(part, []).append(overlap.metric)
    return greyed


if __name__ == "__main__":  # a formula check, for when a team writes their own
    import sys

    if len(sys.argv) < 2:
        print("usage: formula.py 'EXPRESSION'", file=sys.stderr)
        raise SystemExit(2)
    try:
        f = Formula("cli", sys.argv[1])
    except FormulaError as exc:
        print(f"REJECTED {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"OK    {f.expression}")
    print(f"deps  {f.deps.as_json()}")
