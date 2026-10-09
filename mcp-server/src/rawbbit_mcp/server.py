from __future__ import annotations

import hmac
import logging
import re
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware as FastMCPMiddleware, MiddlewareContext
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount
from starlette.applications import Starlette

from rawbbit_mcp.datasets import DatasetRegistry, RuntimeDataset, load_dataset_registry
from rawbbit_mcp.settings import Settings, get_settings
from rawbbit_mcp.tools import GatewayFactory, register_tools

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s service=rawbbit-mcp event=%(message)s",
)
logger = logging.getLogger("rawbbit_mcp")


class SafeToolAuditMiddleware(FastMCPMiddleware):
    """Log tool execution metadata without arguments, SQL, results, or tokens."""

    def __init__(self, dataset_id: str | None = None, auth_mode: str = "none") -> None:
        self.dataset_id = dataset_id
        self.auth_mode = auth_mode
        self.endpoint_class = "scoped" if dataset_id is not None else "main"

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        params = context.message
        arguments = getattr(params, "arguments", None) or {}
        requested_dataset = arguments.get("dataset_id") if isinstance(arguments, dict) else None
        if self.dataset_id is not None:
            dataset_id = self.dataset_id
        elif requested_dataset is None:
            dataset_id = "default"
        elif isinstance(requested_dataset, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,39}", requested_dataset):
            dataset_id = requested_dataset
        else:
            dataset_id = "invalid"

        actor = "unknown"
        try:
            from fastmcp.server.dependencies import get_http_request

            request = get_http_request()
            label = getattr(request.state, "authenticated_api_key_label", None)
            if isinstance(label, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", label):
                actor = label
            elif self.auth_mode == "jwt":
                actor = "jwt"
            elif self.auth_mode == "none":
                actor = "unauthenticated_dev"
        except (LookupError, RuntimeError):
            pass

        requested_tool = getattr(params, "name", "unknown")
        tool_name = (
            requested_tool
            if isinstance(requested_tool, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", requested_tool)
            else "unknown"
        )
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception:
            logger.info(
                "tool_call endpoint=%s dataset_id=%s tool=%s actor=%s result=exception duration_ms=%.2f",
                self.endpoint_class,
                dataset_id,
                tool_name,
                actor,
                (time.perf_counter() - started) * 1000,
            )
            raise
        logger.info(
            "tool_call endpoint=%s dataset_id=%s tool=%s actor=%s result=%s duration_ms=%.2f",
            self.endpoint_class,
            dataset_id,
            tool_name,
            actor,
            "error" if getattr(result, "is_error", False) else "ok",
            (time.perf_counter() - started) * 1000,
        )
        return result


def _build_auth(cfg: Settings):
    if cfg.auth_mode == "static_tokens":
        return None

    if cfg.jwt_jwks_uri:
        from fastmcp.server.auth import JWTVerifier

        kwargs: dict[str, str] = {"jwks_uri": cfg.jwt_jwks_uri}
        if cfg.jwt_issuer:
            kwargs["issuer"] = cfg.jwt_issuer
        if cfg.jwt_audience:
            kwargs["audience"] = cfg.jwt_audience
        return JWTVerifier(**kwargs)

    if cfg.jwt_public_key:
        from fastmcp.server.auth import JWTVerifier

        kwargs = {"public_key": cfg.jwt_public_key}
        if cfg.jwt_issuer:
            kwargs["issuer"] = cfg.jwt_issuer
        if cfg.jwt_audience:
            kwargs["audience"] = cfg.jwt_audience
        return JWTVerifier(**kwargs)

    logger.warning("auth_disabled reason=no_jwt_verifier_configured")
    return None


def _extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    normalized = token.strip()
    return normalized or None


def _resolve_static_token_label(tokens: dict[str, str], token: str | None) -> str | None:
    if not token:
        return None
    for label, candidate in tokens.items():
        if hmac.compare_digest(candidate, token):
            return label
    return None


class StaticBearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, tokens: dict[str, str]) -> None:
        super().__init__(app)
        self.tokens = tokens

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)

        token = _extract_bearer_token(request.headers.get("authorization"))
        label = _resolve_static_token_label(self.tokens, token)
        if label is None:
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        request.state.authenticated_api_key_label = label
        return await call_next(request)


def _middleware_for_tokens(tokens: dict[str, str]) -> list[Middleware]:
    if not tokens:
        return []
    return [Middleware(StaticBearerAuthMiddleware, tokens=tokens)]


def create_app(
    cfg: Settings,
    registry: DatasetRegistry,
    *,
    gateway_factory: GatewayFactory | None = None,
) -> tuple[FastMCP, Any]:
    """Build the backward-compatible main endpoint and isolated scoped endpoints."""
    main_server = FastMCP(cfg.mcp_name, auth=_build_auth(cfg))
    main_server.add_middleware(SafeToolAuditMiddleware(auth_mode=cfg.auth_mode))
    register_tools(main_server, cfg, registry, **({"gateway_factory": gateway_factory} if gateway_factory else {}))
    main_http_app = main_server.http_app(
        path=cfg.mcp_path,
        middleware=_middleware_for_tokens(cfg.api_keys_by_user),
    )

    if not registry.datasets:
        return main_server, main_http_app

    routes: list[Mount] = []
    child_apps = []
    for dataset in registry.datasets:
        child_server = FastMCP(f"{cfg.mcp_name} dataset {dataset.id}")
        child_server.add_middleware(
            SafeToolAuditMiddleware(dataset_id=dataset.id, auth_mode="static_tokens")
        )
        register_tools(
            child_server,
            cfg,
            registry,
            scoped_dataset=dataset,
            **({"gateway_factory": gateway_factory} if gateway_factory else {}),
        )
        child_app = child_server.http_app(
            path=cfg.mcp_path,
            middleware=_middleware_for_tokens(dataset.token_values),
        )
        mount_prefix = dataset.endpoint_path[: -len(cfg.mcp_path.rstrip("/") or "/mcp")].rstrip("/")
        routes.append(Mount(mount_prefix, app=child_app))
        child_apps.append(child_app)

    # Starlette does not automatically enter nested FastMCP lifespans. Start each
    # child session manager in the outer app so every mounted MCP route is ready.
    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for child_app in child_apps:
                await stack.enter_async_context(child_app.lifespan(app))
            await stack.enter_async_context(main_http_app.lifespan(app))
            yield

    routes.append(Mount("/", app=main_http_app))
    return main_server, Starlette(routes=routes, lifespan=lifespan)


registry = load_dataset_registry(settings.mcp_datasets_file, settings)
mcp, app = create_app(settings, registry)


if __name__ == "__main__":
    logger.info(
        "startup env=%s table=%s host=%s port=%s path=%s auth_mode=%s auth_enabled=%s enabled_datasets=%s",
        settings.env,
        settings.table_ref,
        settings.mcp_host,
        settings.mcp_port,
        settings.mcp_path,
        settings.auth_mode,
        settings.auth_mode != "none",
        len(registry.datasets),
    )
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port)
