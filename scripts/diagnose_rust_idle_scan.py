"""Single-expression httpcore 1.0.9 experiment, never a production dependency patch."""
from __future__ import annotations

import ast
from contextlib import ExitStack
import hashlib
import inspect
import textwrap
from types import MethodType
from unittest.mock import patch

import httpcore


def candidate_method():
    original = httpcore.AsyncConnectionPool._assign_requests_to_connections
    source = textwrap.dedent(inspect.getsource(original))
    target = ast.parse("len([connection.is_idle() for connection in self._connections])", mode="eval").body
    replacement = ast.parse("len(self._connections)", mode="eval").body
    expected = ast.dump(target)

    class ReplaceScan(ast.NodeTransformer):
        count = 0

        def visit_Call(self, node):
            if ast.dump(node) == expected:
                self.count += 1
                return ast.copy_location(replacement, node)
            return self.generic_visit(node)

    transform = ReplaceScan()
    tree = transform.visit(ast.parse(source))
    if httpcore.__version__ != "1.0.9" or transform.count != 1:
        raise RuntimeError("idle scan experiment requires the verified dependency expression")
    ast.fix_missing_locations(tree)
    # Compile the installed implementation with exactly one expression replaced.
    # Retain its selection/eviction behavior, including its existing limitations.
    namespace = dict(original.__globals__)
    exec(compile(tree, "<httpcore-single-idle-scan-experiment>", "exec"), namespace)
    candidate = namespace[original.__name__]
    candidate._t4_timing_source = ast.unparse(tree)
    return original, candidate, {"expression_replacements": transform.count,
        "original_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "candidate_sha256": hashlib.sha256(candidate._t4_timing_source.encode()).hexdigest()}


class ElideIdleScan:
    def __init__(self, pools):
        self.pools = list(pools)

    def __enter__(self):
        original, candidate, self.identity = candidate_method()
        if not self.pools or len({id(pool) for pool in self.pools}) != len(self.pools):
            raise ValueError("unique explicit pools required")
        for pool in self.pools:
            if (type(pool) is not httpcore.AsyncConnectionPool or pool._requests or pool._http2
                    or not pool._http1 or pool._proxy is not None
                    or getattr(pool._assign_requests_to_connections, "__func__", None) is not original):
                raise RuntimeError("only unmodified quiescent direct HTTP/1 pools supported")
        self.patches = ExitStack()
        try:
            for pool in self.pools:
                self.patches.enter_context(patch.object(pool, "_assign_requests_to_connections",
                                                       MethodType(candidate, pool)))
        except BaseException:
            self.patches.close()
            raise
        return self

    def __exit__(self, *args):
        self.patches.close()
