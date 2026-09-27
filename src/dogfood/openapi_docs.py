"""Request bodies and responses for /api/v1 in /openapi.json (contracts/t4-api-webhooks-bulk.md case 1).

Handlers read raw JSON or form bodies (one handler serves the HTML form and its API twin), so
FastAPI cannot infer the bodies. This table documents them; `enrich` merges it into the
generated spec. Documentation only: it never changes what a route accepts.
"""

S, I, B, N = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}, {"type": "number"}
TS = {"type": "string", "format": "date-time", "example": "2026-03-01T18:00:00Z"}


def obj(required=(), **props):
    out = {"type": "object", "properties": props}
    if required:
        out["required"] = list(required)
    return out


ERROR = obj(["error"], error=S, detail=S)
PROJECT_BODY = obj(["title"], title={**S, "maxLength": 200}, summary={**S, "maxLength": 2000},
                   repo_url={**S, "description": "http(s) URL"}, track_id=S,
                   draft={**B, "description": "save without submitting"})
REDIRECT = {"303": "Done; redirects to the matching page"}

# (method, path) -> (request body schema or None, {status: description or (description, schema)})
OPS = {
    ("post", "/api/v1/projects"): (PROJECT_BODY, {"201": ("Created", obj(id=S, title=S, status=S)),
                                                  "403": "submissions_closed / not in a team"}),
    ("put", "/api/v1/projects/{project_id}"): (PROJECT_BODY, {"200": "Updated", "403": "submissions_closed / not yours"}),
    ("post", "/api/v1/teams"): (obj(name={**S, "maxLength": 80}), {**REDIRECT, "409": "already in a team"}),
    ("post", "/api/v1/teams/leave"): (None, {"204": "Left", "409": "last_member_with_projects"}),
    ("post", "/api/v1/teams/invite"): (None, {"200": ("New invite link; the old one stops working",
                                                     obj(invite=S))}),
    ("post", "/api/v1/join/{code}"): (None, {**REDIRECT, "404": "unknown invite", "409": "already in a team / full"}),
    ("post", "/api/v1/judge/scores/{project_id}"): (
        {"type": "object", "additionalProperties": {"type": "integer", "minimum": 1, "maximum": 5},
         "properties": {"comment": {**S, "maxLength": 4000}},
         "description": "one integer 1-5 per rubric criterion, e.g. {\"functionality\": 4, ...}"},
        {"200": "Saved", "403": "not assigned / conflict_of_interest / judging_closed"}),
    ("post", "/api/v1/assignments/tiebreak"): (obj(k={**I, "minimum": 1, "maximum": 50}), REDIRECT),
    ("post", "/api/v1/assignments/auto"): (obj(k={**I, "minimum": 1, "maximum": 10, "description": "judges per project"}),
                                           REDIRECT),
    ("post", "/api/v1/judges/{judge_id}/exclude"): (obj(["reason"], reason=S), REDIRECT),
    ("post", "/api/v1/judges/{judge_id}/include"): (None, REDIRECT),
    ("post", "/api/v1/judges"): (obj(["email"], email=S, name=S, tracks={**S, "description": "comma-separated track ids"}),
                                 REDIRECT),
    ("post", "/api/v1/event"): (obj(name=S, prizes=S, submissions_close=TS, judging_close=TS, voting_open=TS,
                                    voting_close=TS, votes_per_voter={**I, "minimum": 1, "maximum": 100}),
                                {**REDIRECT, "422": ("invalid", ERROR)}),
    ("post", "/api/v1/events"): (obj(["submissions_close"], name=S, submissions_close=TS, judging_close=TS, prizes=S,
                                     tracks={**S, "description": "comma-separated names"}),
                                 {**REDIRECT, "403": "admins only"}),
    ("post", "/api/v1/rubric"): ({"type": "object", "additionalProperties": N,
                                  "description": "w_<criterion>: weight in (0, 1000]"}, REDIRECT),
    ("post", "/api/v1/results/publish"): (obj(unpublish=B), REDIRECT),
    ("post", "/api/v1/votes"): (obj(["project"], project=S),
                                {"201": ("Counted", obj(project=S, voted=B)), "403": "voting_closed / judges_do_not_vote / "
                                 "conflict_of_interest", "404": "not_on_ballot", "409": "already_voted / vote_limit",
                                 "429": "rate limited (Retry-After)"}),
    ("delete", "/api/v1/votes/{project_id}"): (None, {"204": "Withdrawn", "404": "no_such_vote"}),
    ("post", "/api/v1/voters/{user_id}/void"): (obj(["reason"], reason=S), REDIRECT),
    ("post", "/api/v1/voters/{user_id}/unvoid"): (None, REDIRECT),
    ("post", "/api/v1/projects/{project_id}/comments"): (obj(["body"], body={**S, "minLength": 1, "maxLength": 2000}),
                                                         {"201": ("Created", obj(id=I, project=S, body=S)),
                                                          "429": "rate limited"}),
    ("delete", "/api/v1/comments/{comment_id}"): (None, {"204": "Deleted", "403": "not yours"}),
    ("post", "/api/v1/records/issue"): (None, {"200": ("Issued", obj(issued=I)), "409": "results_not_published"}),
    ("post", "/api/v1/records/{record_id}/revoke"): (obj(["reason"], reason=S), {"204": "Revoked",
                                                                               "409": "already_revoked"}),
    ("post", "/api/v1/tokens"): (obj(name=S), {"201": ("Shown once", obj(id=S, name=S, token=S))}),
    ("delete", "/api/v1/tokens/{token_id}"): (None, {"204": "Revoked"}),
    ("post", "/api/v1/webhooks"): (obj(["url", "secret", "events"], url=S, secret={**S, "minLength": 16},
                                       events={"type": "array", "items": {"type": "string", "enum": [
                                           "project.submitted", "project.updated", "score.submitted",
                                           "results.published", "voting.closed"]}}),
                                   {"201": ("Created (the secret is never returned)", obj(id=S, url=S, events={
                                       "type": "array", "items": S})), "422": ("invalid", ERROR)}),
    ("delete", "/api/v1/webhooks/{webhook_id}"): (None, {"204": "Deleted"}),
    ("post", "/api/v1/import"): ({"type": "object", "required": ["event"], "description":
                                  "fixtures.json shape, or an export from GET /api/v1/events/{id}/export.json"},
                                 {"201": "Imported", "409": "event_exists / ids_in_use", "422": ("invalid", ERROR)}),
}

# The HTML twin of POST /api/v1/votes takes the project in its path; the API takes it in the body.
DROP_PARAMS = {("post", "/api/v1/votes"): {"project_id"}}
EVENT_PARAM = {"name": "event", "in": "query", "required": False, "schema": S,
               "description": "event id; default: the first event"}
NO_EVENT_PARAM = {"/api/v1/events", "/api/v1/events/{event_id}/export.json", "/api/v1/import", "/api/v1/tokens",
                  "/api/v1/tokens/{token_id}", "/api/v1/projects/{project_id}/comments", "/api/v1/comments/{comment_id}",
                  "/api/v1/records/{record_id}/revoke", "/api/v1/join/{code}"}


def enrich(spec: dict) -> dict:
    spec["components"] = spec.get("components") or {}
    spec["components"]["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer",
                                                        "description": "POST /api/v1/tokens"}}
    for path, item in spec.get("paths", {}).items():
        if not path.startswith("/api/v1"):
            continue
        for method, op in item.items():
            op["security"] = [{"bearer": []}, {}]
            params = [p for p in op.get("parameters", []) if p["name"] not in DROP_PARAMS.get((method, path), set())]
            if path not in NO_EVENT_PARAM and not any(p["name"] == "event" for p in params):
                params.append(EVENT_PARAM)
            op["parameters"] = params
            body, responses = OPS.get((method, path), (None, {}))
            if body is not None:
                op["requestBody"] = {"required": True, "content": {
                    "application/json": {"schema": body},
                    "application/x-www-form-urlencoded": {"schema": body}}}
            for status, value in responses.items():
                desc, schema = value if isinstance(value, tuple) else (value, None)
                entry = {"description": desc}
                if schema is not None:
                    entry["content"] = {"application/json": {"schema": schema}}
                op.setdefault("responses", {})[status] = entry
            if any(s != "200" and s.startswith("2") or s == "303" for s in responses):
                op["responses"].pop("200", None)
            if method != "get" and "422" in op["responses"] and "422" not in responses:
                op["responses"]["422"] = {"description": "invalid body",
                                          "content": {"application/json": {"schema": ERROR}}}
            op["responses"].setdefault("401", {"description": "not logged in"})
    return spec
