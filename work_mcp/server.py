"""Private, single-owner MCP adapter for the existing Calorie Bridge REST API."""
import hashlib
import html
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import (
    AuthorizationCode, AuthorizationParams, RefreshToken, AccessToken,
    AuthorizeError, RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.types import ToolAnnotations

SCOPE = "meals"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    """Persistent OAuth state; opaque credentials are indexed by SHA-256."""
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS state (kind TEXT, key TEXT, data TEXT, expires REAL, PRIMARY KEY(kind,key))")
        os.chmod(path, 0o600)

    def db(self):
        return sqlite3.connect(self.path, timeout=10)

    def put(self, kind, key, data, ttl):
        with self.db() as db:
            db.execute("DELETE FROM state WHERE expires < ?", (time.time(),))
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?,?,?)", (kind, digest(key), json.dumps(data), time.time()+ttl))

    def get(self, kind, key, consume=False):
        with self.db() as db:
            if consume:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM state WHERE kind=? AND key=? AND expires>?", (kind, digest(key), time.time())).fetchone()
            if consume and row:
                db.execute("DELETE FROM state WHERE kind=? AND key=?", (kind, digest(key)))
            return json.loads(row[0]) if row else None

    def count(self, kind):
        with self.db() as db:
            return db.execute("SELECT COUNT(*) FROM state WHERE kind=? AND expires>?", (kind,time.time())).fetchone()[0]


class Provider:
    def __init__(self, store, base):
        self.store, self.base = store, base
        self.resource = base + "/mcp"

    async def get_client(self, client_id):
        data = self.store.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        # Private ChatGPT integration: never redirect owner credentials/codes to arbitrary sites.
        for uri in client_info.redirect_uris or []:
            u = urlsplit(str(uri))
            allowed = u.path == "/connector_platform_oauth_redirect" or u.path.startswith("/connector/oauth/")
            if u.scheme != "https" or u.netloc != "chatgpt.com" or not allowed or u.fragment or u.query:
                raise RegistrationError("invalid_redirect_uri", "Only ChatGPT connector callbacks are allowed")
        if not client_info.redirect_uris or client_info.token_endpoint_auth_method != "none":
            raise RegistrationError("invalid_client_metadata", "Use a public client with token_endpoint_auth_method=none and PKCE")
        if self.store.count("client") >= 1000:
            raise RegistrationError("invalid_client_metadata", "Client registration limit reached")
        self.store.put("client", client_info.client_id, client_info.model_dump(mode="json"), 365*86400)

    async def authorize(self, client, params):
        if params.resource != self.resource or set(params.scopes or [SCOPE]) != {SCOPE}:
            raise AuthorizeError("invalid_request", "Expected this MCP resource and meals scope")
        ticket = secrets.token_urlsafe(32)
        self.store.put("pending", ticket, {"client": client.client_id, "params": params.model_dump(mode="json")}, 600)
        return self.base + "/login?ticket=" + ticket

    async def load_authorization_code(self, client, code):
        data = self.store.get("code", code)
        if data and data["client_id"] == client.client_id:
            return AuthorizationCode(code=code, **data)

    def issue(self, client_id, scopes, resource, family=None):
        family = family or secrets.token_urlsafe(32)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        info = {"client_id":client_id, "scopes":scopes, "resource":resource, "subject":"owner", "family":family}
        self.store.put("access", access, {**info, "expires_at":int(time.time())+3600}, 3600)
        self.store.put("refresh", refresh, {**info, "expires_at":int(time.time())+30*86400}, 30*86400)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600, refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        if not self.store.get("code", authorization_code.code, consume=True):
            raise TokenError("invalid_grant", "Code already used or expired")
        return self.issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, token):
        data = self.store.get("refresh", token)
        if data and data["client_id"] == client.client_id and not self.store.get("revoked", data["family"]):
            return RefreshToken(token=token, **{k:v for k,v in data.items() if k != "family"})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        data = self.store.get("refresh", refresh_token.token, consume=True)
        if not data:
            raise TokenError("invalid_grant", "Refresh token already used or expired")
        return self.issue(client.client_id, scopes, refresh_token.resource, data["family"])

    async def load_access_token(self, token):
        data = self.store.get("access", token)
        if data and not self.store.get("revoked", data["family"]) and data["resource"] == self.resource:
            return AccessToken(token=token, **{k:v for k,v in data.items() if k != "family"})

    async def revoke_token(self, token):
        data = self.store.get("access", token.token) or self.store.get("refresh", token.token)
        if data:
            self.store.put("revoked", data["family"], True, 31*86400)


def build_app(config, transport=None):
    base = config["public_url"].rstrip("/")
    if not base.startswith("https://") or urlsplit(base).path:
        raise ValueError("public_url must be an HTTPS origin")
    store = Store(config["state_path"])
    provider = Provider(store, base)
    api_key = config["api_key"]
    if not api_key or api_key == "change-me":
        raise ValueError("Configure a real bridge API key")
    bridge = config.get("bridge_url", "http://127.0.0.1:8021").rstrip("/")
    if bridge != "http://127.0.0.1:8021":
        raise ValueError("This deployment expects the existing bridge at 127.0.0.1:8021")

    async def api(method, path, **kwargs):
        async with httpx.AsyncClient(transport=transport, timeout=20, follow_redirects=False) as client:
            response = await client.request(method, bridge+path, headers={"X-API-Key":api_key}, **kwargs)
            response.raise_for_status()
            return response.json()

    mcp = FastMCP("Calorie Logger", auth_server_provider=provider,
        auth=AuthSettings(issuer_url=AnyHttpUrl(base), resource_server_url=AnyHttpUrl(provider.resource),
            validate_token_resource=True, required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(enabled=True,valid_scopes=[SCOPE],default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True)),
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[urlsplit(base).netloc, "127.0.0.1:8023"], allowed_origins=[base]))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def getDailySummary(day: date | None = None) -> dict:
        """Read actual totals. Omit day for today in America/Toronto; never infer totals from chat."""
        return await api("GET", "/api/summary", params={"day":day.isoformat()} if day else {})

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def getMeals(day: date | None = None) -> dict:
        """Read up to 50 meals for one Toronto day, newest first. Check after an uncertain write."""
        rows = await api("GET", "/api/meals", params={"limit":50, **({"day":day.isoformat()} if day else {})})
        return {"meals":rows, "limit":50, "possibly_truncated":len(rows)==50}

    async def checked_result(result):
        try:
            checked = await api("GET", f"/api/meals/{int(result['id'])}/verification")
            if not isinstance(checked, dict) or "fatsecret" not in checked:
                raise ValueError("Missing verification result")
            return {**result, "verification": checked,
                    "reporting_instruction": "Report Pi saved and FatSecret status separately. Only status=verified confirms FatSecret sync."}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return {**result, "verification": {"local_saved": True,
                    "fatsecret": {"status": "unverified", "error": "Verification unavailable; meal is saved on Pi. Do not log it again."}},
                    "reporting_instruction": "Report Pi saved; FatSecret not confirmed. Use getMealSyncStatus to recheck."}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    async def getMealSyncStatus(meal_id: Annotated[int, Field(gt=0)]) -> dict:
        """Check the saved Pi meal and read back any attempted FatSecret diary write.
        Mandatory after logging: report both statuses. Only verified confirms FatSecret.
        Pending/retrying/verifying is not success. Never call logMeal again to retry sync.
        """
        return await api("GET", f"/api/meals/{meal_id}/verification")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def retryMealSync(meal_id: Annotated[int, Field(gt=0)]) -> dict:
        """Requeue FatSecret sync for an existing Pi meal, without creating another meal.
        Unknown diary writes are read back, never blindly reposted. Legacy/ambiguous
        records may require review. Use after connection/permission problems are fixed.
        """
        return await api("POST", f"/api/meals/{meal_id}/retry-sync")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def logMeal(
        request_id: Annotated[str, Field(min_length=16, max_length=100)],
        name: Annotated[str, Field(min_length=1, max_length=200)],
        calories: Annotated[float, Field(ge=0, allow_inf_nan=False)],
        protein: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0,
        carbs: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0,
        fat: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0,
        fiber: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0,
        sugar: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0,
        meal_type: Literal["breakfast", "lunch", "dinner", "other"] = "other",
        notes: Annotated[str, Field(max_length=500)] = "",
        eaten_at: datetime | None = None,
        fatsecret_search_query: Annotated[str | None, Field(max_length=200)] = None,
    ) -> dict:
        """Save one meal and check Pi/FatSecret status separately. Only verified means synced.
        Always call getMealSyncStatus afterward and report its result, plus getDailySummary.
        Pending or unverified means saved on Pi only. Use a unique request_id for each intended meal.
        Reuse the SAME ID on retries. Never retry with a new ID after a timeout; inspect getMeals first.
        Optional eaten_at must include a timezone. Idempotency records are retained indefinitely.
        """
        if eaten_at is not None and eaten_at.utcoffset() is None:
            raise ValueError("eaten_at must include its timezone offset")
        payload = {"name":name, "calories":calories, "protein":protein, "carbs":carbs,
            "fat":fat, "fiber":fiber, "sugar":sugar, "meal_type":meal_type, "notes":notes}
        if eaten_at:
            payload["eaten_at"] = eaten_at.isoformat()
        if fatsecret_search_query:
            payload["fatsecret_search_query"] = fatsecret_search_query
        key = digest(request_id)
        fingerprint = digest(json.dumps(payload, sort_keys=True))
        with store.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS writes (key TEXT PRIMARY KEY, fingerprint TEXT, result TEXT)")
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT fingerprint,result FROM writes WHERE key=?", (key,)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ValueError("request_id already used with different meal details")
                if old[1] is None:
                    raise ValueError("Write outcome uncertain or in progress. Inspect getMeals; do not submit a new ID automatically.")
            else:
                db.execute("INSERT INTO writes VALUES (?,?,NULL)", (key, fingerprint))
        if old:
            return await checked_result(json.loads(old[1]))
        # Never automatically retry POST: the existing API has no idempotency key support.
        try:
            result = await api("POST", "/api/meals", json=payload)
        except Exception as exc:
            raise ValueError("Meal save not confirmed. Inspect getMeals before any further write.") from exc
        with store.db() as db:
            db.execute("UPDATE writes SET result=? WHERE key=?", (json.dumps(result),key))
        return await checked_result(result)

    # Use the SDK's OAuth/PKCE implementation, with public-client metadata for ChatGPT.
    app = mcp.streamable_http_app()
    metadata_route = next(r for r in app.routes if getattr(r,"path","") == "/.well-known/oauth-authorization-server")
    from mcp.server.auth.routes import build_metadata
    metadata = build_metadata(AnyHttpUrl(base), None, mcp.settings.auth.client_registration_options, mcp.settings.auth.revocation_options).model_dump(mode="json", exclude_none=True)
    metadata["token_endpoint_auth_methods_supported"] = ["none"]
    metadata["revocation_endpoint_auth_methods_supported"] = ["none"]
    # SDK issuer normalizes trailing slash; use that exact string in authorization responses.
    async def metadata_handler(request):
        return JSONResponse(metadata, headers={"Cache-Control":"no-store", "Access-Control-Allow-Origin":"*"})
    app.routes.remove(metadata_route)
    # Discovery clients differ in whether they retain the issuer trailing slash
    # or use the MCP URL as their initial issuer candidate. Serve metadata directly
    # at these aliases instead of relying on redirects or returning a 404.
    metadata["code_challenge_methods_supported"] = ["S256"]
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/",
                 "/.well-known/oauth-authorization-server/mcp", "/mcp/.well-known/oauth-authorization-server"):
        app.routes.insert(0, Route(path, metadata_handler))

    async def resource_metadata(request):
        return JSONResponse({"resource":provider.resource,"authorization_servers":[metadata["issuer"]],
            "scopes_supported":[SCOPE],"bearer_methods_supported":["header"]},
            headers={"Cache-Control":"no-store","Access-Control-Allow-Origin":"*"})
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/"):
        app.routes.insert(0, Route(path, resource_metadata))

    async def health(request):
        return JSONResponse({"status":"ok", "service":"calorie-work-mcp", "version":"1.1.0"})

    async def login(request):
        ticket = request.query_params.get("ticket", "")
        pending = store.get("pending", ticket)
        if not pending:
            return HTMLResponse("Sign-in expired. Reconnect Calorie Logger in ChatGPT.", status_code=400)
        nonce = secrets.token_urlsafe(32)
        store.put("csrf", ticket, digest(nonce), 600)
        body = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Connect Calorie Logger</title>
        <body><main><h1>Connect Calorie Logger</h1><p>Authorize ChatGPT to read your meals and daily totals, and log meals to your Raspberry Pi. Logging can also sync to FatSecret.</p>
        <p>Use your existing calorie dashboard username and password.</p>
        <form method="post" action="/approve"><input type="hidden" name="ticket" value="{html.escape(ticket,quote=True)}"><input type="hidden" name="csrf" value="{nonce}">
        <p><label>Username <input name="username" autocomplete="username" required maxlength="200"></label></p>
        <p><label>Password <input name="password" type="password" autocomplete="current-password" required maxlength="1000"></label></p>
        <button type="submit">Sign in and authorize</button></form><p>Close this page to cancel.</p></main></body></html>'''
        response = HTMLResponse(body)
        response.set_cookie("__Host-calorie-csrf", nonce, max_age=600, secure=True, httponly=True, samesite="strict", path="/")
        return response

    async def approve(request):
        if request.headers.get("origin") != base:
            return HTMLResponse("Invalid sign-in origin", status_code=403)
        form = await request.form()
        ticket, csrf = str(form.get("ticket","")), str(form.get("csrf",""))
        saved = store.get("csrf", ticket)
        if not saved or not secrets.compare_digest(saved,digest(csrf)) or not secrets.compare_digest(csrf,request.cookies.get("__Host-calorie-csrf","")):
            return HTMLResponse("Sign-in expired. Reconnect in ChatGPT.", status_code=403)
        pending = store.get("pending",ticket)
        if not pending:
            return HTMLResponse("Sign-in expired",status_code=400)
        # One owner; global bounded attempts also apply when requests pass through Funnel.
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM state WHERE kind='attempt' AND expires < ?",(time.time(),))
            if db.execute("SELECT COUNT(*) FROM state WHERE kind='attempt'").fetchone()[0] >= 10:
                return HTMLResponse("Too many attempts. Wait 10 minutes.",status_code=429)
            db.execute("INSERT INTO state VALUES ('attempt',?,'null',?)",(secrets.token_hex(16),time.time()+600))
        try:
            async with httpx.AsyncClient(transport=transport,timeout=15,follow_redirects=False) as client:
                result = await client.get(bridge+"/",auth=(str(form.get("username","")),str(form.get("password",""))))
        except httpx.HTTPError:
            return HTMLResponse("Dashboard unavailable. Try again later.",status_code=503)
        if result.status_code != 200:
            return HTMLResponse("Sign-in failed. Go back and try your dashboard login.",status_code=401)
        pending = store.get("pending",ticket,consume=True)
        if not pending:
            return HTMLResponse("Sign-in already used",status_code=400)
        store.get("csrf",ticket,consume=True)
        params = AuthorizationParams.model_validate(pending["params"])
        code = secrets.token_urlsafe(32)
        data = AuthorizationCode(code=code,client_id=pending["client"],scopes=params.scopes or [SCOPE],
            expires_at=time.time()+120,code_challenge=params.code_challenge,redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,resource=params.resource,subject="owner")
        store.put("code",code,data.model_dump(mode="json",exclude={"code"}),120)
        response = RedirectResponse(construct_redirect_uri(str(params.redirect_uri),code=code,state=params.state,iss=metadata["issuer"]),status_code=303)
        response.delete_cookie("__Host-calorie-csrf",secure=True,httponly=True,samesite="strict",path="/")
        return response

    # The SDK v1 token handler does not check the supplied resource parameter;
    # require exact audience binding before delegating PKCE/client validation.
    from mcp.server.auth.handlers.token import TokenHandler
    from mcp.server.auth.middleware.client_auth import ClientAuthenticator
    token_handler = TokenHandler(provider, ClientAuthenticator(provider))
    async def token_endpoint(request):
        form = await request.form()
        if form.get("resource") != provider.resource:
            return JSONResponse({"error":"invalid_target"},status_code=400)
        return await token_handler.handle(request)

    async def revoke(request):
        form = await request.form()
        cid, raw = str(form.get("client_id","")), str(form.get("token",""))
        client = await provider.get_client(cid)
        if not client:
            return JSONResponse({"error":"invalid_client"},status_code=401)
        token = await provider.load_access_token(raw) or await provider.load_refresh_token(client,raw)
        if token and token.client_id == cid:
            await provider.revoke_token(token)
        return JSONResponse({})

    app.routes[:] = [r for r in app.routes if getattr(r,"path","") not in {"/token","/revoke"}]
    app.routes.extend([Route("/token",token_endpoint,methods=["POST"]),Route("/revoke",revoke,methods=["POST"]),
        Route("/health",health),Route("/login",login),Route("/approve",approve,methods=["POST"])])

    # Bound all request bodies; suppress caching/referrers on every auth response.
    from mcp.server.transport_security import RequestBodyLimitMiddleware
    inner = RequestBodyLimitMiddleware(app, 65536)
    async def secured(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope,receive,send)
        # Log only fixed route labels. Never log a raw URL, query string,
        # headers, body, credentials, authorization codes or arbitrary paths.
        path = scope.get("path", "")
        known_paths = {getattr(route, "path", "") for route in app.routes}
        safe_path = path if path in known_paths else "<other>"
        method = scope.get("method", "")
        safe_method = method if method in {"GET", "POST", "HEAD", "OPTIONS", "DELETE", "PUT", "PATCH"} else "OTHER"
        async def safe_send(message):
            if message["type"] == "http.response.start":
                logging.getLogger("uvicorn.error").info(
                    "MCP request method=%s path=%s status=%s",
                    safe_method, safe_path, message["status"],
                )
                message.setdefault("headers",[]).extend([(b"cache-control",b"no-store"),(b"referrer-policy",b"strict-origin"),
                    (b"x-content-type-options",b"nosniff"),(b"content-security-policy",b"default-src 'none'; form-action 'self' https://chatgpt.com; frame-ancestors 'none'; base-uri 'none'")])
            await send(message)
        await inner(scope,receive,safe_send)
    return secured


def from_environment():
    config = json.loads(Path(os.environ.get("MCP_CONFIG","/config/config.json")).read_text())
    config["state_path"] = os.environ.get("MCP_STATE_PATH","/data/oauth.sqlite3")
    return build_app(config)
