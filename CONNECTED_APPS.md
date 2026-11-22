# Connected apps (OAuth)

Lets a person connect Claude, or any MCP client, to their own account without giving it a password. The app acts as
that person, with a scope they choose, until they disconnect it. This is the backend half; the MCP server that uses it
over HTTP and the "Connect MCP" screens come next.

## How a connection is made

1. The app registers itself: `POST /api/v1/oauth/register` (name and redirect addresses). It gets a `client_id`, no secret.
2. It sends the person's browser to `GET /api/v1/oauth/authorize` (PKCE `S256` is required). The API stores the request
   for ten minutes and redirects to the frontend's consent screen, `FRONTEND_URL` + `OAUTH_CONSENT_PATH?request=<id>`.
3. The person is signed in as usual (Google, password). The screen calls `GET /oauth/requests/{id}` (app name, the
   address it returns to, the scopes this person may give) and then `POST /oauth/requests/{id}/approve` with the chosen
   scope, or `/deny`. Both answer `{"redirect_url": ...}`; the screen sends the browser there. Approving returns the
   app a one-time code valid for two minutes.
4. The app swaps the code for tokens at `POST /oauth/token` (form fields, with its `code_verifier`).
5. It calls the API with `Authorization: Bearer <access_token>`. The access token lasts an hour. `grant_type=refresh_token`
   gives a new pair; the old refresh token stops working. Using a refresh token that was already replaced closes the
   whole connection (a copy was stolen, or the app has a bug).

Apps find all this at `GET /.well-known/oauth-authorization-server` (at the server root). If the API is behind a proxy,
let that path through and set `OAUTH_ISSUER` to the public address (`https://syllabus.narxoz.kz`).

## Scopes

| Scope | What it opens | Who may give it |
|---|---|---|
| `mcp:read` | The read endpoints: nothing changes, nothing costs money. | Every user |
| `mcp:write` | + change study plans, set a course's programs, comment, mark notifications read. | Every user (each endpoint still checks the role) |
| `mcp:full` | + start AI checks (they cost money), move a syllabus through approval, e-mail the review report. | Administrator, Provost Office, KOK |

The lists are `app/auth/scopes.py`; the MCP server builds its tools from the same lists. A token may call **only** what
its scope lists. Every other endpoint, including `/admin`, `/auth`, `/drive`, uploads and downloads, and the `/oauth`
screens themselves, answers 403 to a connected app, whatever the user's role is. So an app cannot approve or widen a
connection on its own.

The person's role is checked on top, at every call, not once when the token was made: taking a role away, or switching
the account off, narrows or ends the connection at once. The app may ask for more than a person can give; the screen
offers what their role allows, defaulting to what was asked for lowered to that.

## For the person

`GET /oauth/grants` lists their connected apps (name, scope, when it was last used); `DELETE /oauth/grants/{id}`
disconnects one. An app can also end its own connection with `POST /oauth/revoke` (RFC 7009).

## What is stored

`oauth_clients` (registered apps) and `oauth_grants` (one row per connection: user, app, scope, the SHA-256 of the
current access and refresh tokens, expiry, last use, revoked). The tokens themselves are never stored. Pending requests
and codes live in Redis and are used once.

## Limits and choices

- Redirect addresses must be `https`, or `http` on localhost (the port may change from one connection to the next).
  Custom schemes such as `cursor://` are refused for now.
- Registration is open (that is how Claude connects without setup), limited to 30 per hour per address. The app's name is its
  own claim, so the consent screen also shows the address it returns to.
- Settings: `OAUTH_ISSUER`, `OAUTH_CONSENT_PATH` (default `/connect-mcp`), `OAUTH_ACCESS_TTL_S` (3600),
  `OAUTH_REFRESH_TTL_DAYS` (60).
- Not done yet: the `resource` parameter (RFC 8707) is kept but not checked, and nobody can see all connections of
  all users (an administrator list would be a small addition).
