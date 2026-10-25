"""Helpers for `test_adversarial_access_control.py`: route discovery straight from the application object.

Routes are discovered by walking the router tree (not docs/api.md and not the OpenAPI schema, which omits
`include_in_schema=False` operations), so a newly added endpoint cannot escape the authentication, CSRF and
permission sweeps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class RouteInfo:
    method: str
    path: str  # full path including the /api/v1 prefix, with `{param}` placeholders
    route: APIRoute

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)

    @property
    def unsafe(self) -> bool:
        return self.method in UNSAFE_METHODS

    @property
    def path_params(self) -> list[str]:
        return list(self.route.param_convertors)

    @property
    def body_model(self) -> str | None:
        if self.route.body_field is None:
            return None
        annotation = self.route.body_field.field_info.annotation
        return getattr(annotation, "__name__", str(annotation))

    def query_params(self) -> set[str]:
        return {p.name for p in _all_query_params(self.route.dependant)}

    def depends_on(self, call: Callable[..., Any]) -> bool:
        return call in set(_calls(self.route.dependant))

    def concrete(self, ids: dict[str, str]) -> str:
        return self.path.format(**{name: ids[name] for name in self.path_params})

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


def _walk(routes: Iterable[BaseRoute], prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif hasattr(route, "original_router") and hasattr(route, "include_context"):
            # FastAPI >= 0.16x keeps included routers as nodes; the include context carries the parent prefix.
            yield from _walk(route.original_router.routes, prefix + route.include_context.prefix)
        else:
            sub = getattr(route, "routes", None)
            if sub:  # starlette Mount / router-like containers
                yield from _walk(sub, prefix + getattr(route, "path", ""))


def discover_routes(app: FastAPI) -> list[RouteInfo]:
    found = {
        (method, path): RouteInfo(method, path, route)
        for path, route in _walk(app.router.routes)
        for method in sorted(route.methods or ())
        if method != "HEAD"
    }
    return [found[k] for k in sorted(found)]


def openapi_operations(app: FastAPI) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        for method in ops
        if method in ("get", "post", "put", "patch", "delete")
    }


def _calls(dependant: Dependant) -> Iterator[Callable[..., Any]]:
    for dep in dependant.dependencies:
        if dep.call is not None:
            yield dep.call
        yield from _calls(dep)


def _all_query_params(dependant: Dependant) -> Iterator[Any]:
    yield from dependant.query_params
    for dep in dependant.dependencies:
        yield from _all_query_params(dep)
