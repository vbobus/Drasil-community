"""Keep Vercel's short-lived OIDC header scoped to the current request."""


class VercelHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from vercel.headers import HeadersContext, headers_from_asgi_scope

        with HeadersContext(headers_from_asgi_scope(scope)).use():
            await self.app(scope, receive, send)
