"""Проверка OIDC из доверенного GitHub Actions без записи токенов на диск."""

import base64
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


def validate_claims(claims, config, environment, audience):
    account = next(a for a in config["accounts"] if a["key"] == f"{environment}_ci")
    checks = {
        "iss": config["federation"]["issuer"],
        "aud": audience,
        "sub": account["subject"],
        "repository": config["repository"],
        "ref": "refs/heads/main",
        "workflow_ref": f"{config['repository']}/.github/workflows/cloud-access.yml@refs/heads/main",
    }
    if any(claims.get(key) != value for key, value in checks.items()):
        raise ValueError("Unexpected OIDC claims")
    if claims.get("event_name") not in {"push", "workflow_dispatch"}:
        raise ValueError("Untrusted workflow event")


def request_json(url, *, headers=None, data=None):
    request = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def verify():
    config = json.loads((Path(__file__).parent / "bootstrap.json").read_text(encoding="utf-8"))
    environment = os.environ["GYM_ENVIRONMENT"]
    if environment not in {"staging", "production"}:
        raise ValueError("Unknown environment")
    audience = os.environ["YC_OIDC_AUDIENCE"]
    if audience != config["federation"]["audience"]:
        raise ValueError("Unexpected audience")
    folder_id = os.environ["YC_FOLDER_ID"]
    other_folder_id = os.environ["YC_OTHER_FOLDER_ID"]
    if not folder_id or not other_folder_id or folder_id == other_folder_id:
        raise ValueError("Invalid folder isolation probe")
    oidc_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    parsed = urllib.parse.urlparse(oidc_url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".actions.githubusercontent.com"):
        raise ValueError("Unexpected GitHub token endpoint")
    separator = "&" if parsed.query else "?"
    response = request_json(
        oidc_url + separator + urllib.parse.urlencode({"audience": audience}),
        headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
    )
    jwt = response["value"]
    payload = jwt.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    # Local checks prevent configuration mistakes; YC verifies the signature.
    validate_claims(claims, config, environment, audience)
    result = request_json(
        "https://auth.yandex.cloud/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": os.environ["YC_SERVICE_ACCOUNT_ID"],
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
        }).encode(),
    )
    headers = {"Authorization": "Bearer " + result["access_token"]}
    base = "https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders/"
    folder = request_json(base + urllib.parse.quote(folder_id, safe=""), headers=headers)
    if folder["id"] != folder_id or folder["cloudId"] != config["cloud_id"]:
        raise ValueError("Unexpected target folder")
    try:
        request_json(base + urllib.parse.quote(other_folder_id, safe=""), headers=headers)
    except urllib.error.HTTPError as error:
        if error.code not in {403, 404}:
            raise
    else:
        raise ValueError("Cross-environment access unexpectedly allowed")
    print(f"OIDC verified for {environment}; cross-environment metadata access denied.")


if __name__ == "__main__":
    try:
        verify()
    except urllib.error.HTTPError as error:
        print(f"OIDC verification failed (HTTP {error.code}); no tokens or response bodies logged.", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f"OIDC verification failed ({type(error).__name__}); no tokens or response bodies logged.", file=sys.stderr)
        sys.exit(1)
