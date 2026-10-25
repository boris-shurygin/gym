"""Создание месячного бюджета Gym; по умолчанию только просмотр плана."""

import argparse
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
BASE = "https://billing.api.cloud.yandex.net/billing/v1"


def desired_spec(config, state, recipient):
    if state["cloud_id"] != config["cloud_id"] or config["monthly_budget_rub"] != 3000:
        raise ValueError("Unreviewed billing configuration")
    return {
        "resetPeriod": "MONTHLY", "amount": str(config["monthly_budget_rub"]),
        "endDate": config["budget_end_date"],
        "notificationUserAccountIds": [recipient],
        "thresholdRules": [{"type": "PERCENT", "amount": str(value)} for value in (80, 95)],
        "filter": {"cloudFoldersFilters": [{"cloudId": config["cloud_id"],
                   "folderIds": sorted(state["folders"][name] for name in ("delivery", "staging", "production"))}]},
    }


def matches_spec(actual, expected):
    # Ignore response-only fields and empty repeated fields omitted by protobuf JSON.
    if Decimal(actual.get("amount", "-1")) != Decimal(expected["amount"]):
        return False
    for key in ("resetPeriod", "endDate", "notificationUserAccountIds"):
        if actual.get(key) != expected[key]:
            return False
    if actual.get("filter", {}).get("serviceIds"):
        return False
    filters = actual.get("filter", {}).get("cloudFoldersFilters", [])
    target = expected["filter"]["cloudFoldersFilters"][0]
    if len(filters) != 1 or filters[0].get("cloudId") != target["cloudId"] or sorted(filters[0].get("folderIds", [])) != target["folderIds"]:
        return False
    rules = actual.get("thresholdRules", [])
    return len(rules) == 2 and sorted((r["type"], Decimal(r["amount"])) for r in rules) == [("PERCENT", Decimal(80)), ("PERCENT", Decimal(95))]


def configure(config, state, recipient, request, apply=False):
    account_id = config["billing_account_id"]
    account = request(f"/billingAccounts/{account_id}")
    if account.get("id") != account_id or not account.get("active") or account.get("currency") != "RUB":
        raise ValueError("Expected active RUB billing account")
    bindings = request(f"/billingAccounts/{account_id}/billableObjectBindings").get("billableObjectBindings", [])
    if not any(item.get("billableObject", {}).get("id") == config["cloud_id"] for item in bindings):
        raise ValueError("Cloud is not linked to the reviewed billing account")
    spec = desired_spec(config, state, recipient)
    name = "gym-monthly-3000-rub"
    response = request(f"/budgets?billingAccountId={account_id}&pageSize=100")
    if response.get("nextPageToken"):
        raise ValueError("Budget pagination requires review")
    found = [b for b in response.get("budgets", []) if b["name"] == name]
    if len(found) > 1 or (found and (found[0].get("status", "ACTIVE") not in {"CREATING", "ACTIVE"} or not matches_spec(found[0].get("costBudget", {}), spec))):
        raise ValueError("Existing budget differs; refusing replacement")
    action = "none" if found else "create"
    if apply and not found:
        operation = request("/budgets", {"billingAccountId": account_id, "name": name, "costBudgetSpec": spec})
        if operation.get("error"):
            raise ValueError("Budget creation operation failed")
    return {"mode": "applied" if apply else "plan", "action": action, "name": name,
            "amount_rub": 3000, "threshold_percent": [80, 95, 100], "end_date": config["budget_end_date"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yc", default=str(ROOT / ".local/bin/yc.exe"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = json.loads((ROOT / "infra/bootstrap.json").read_text(encoding="utf-8"))
    state = json.loads((ROOT / ".local/bootstrap-state.json").read_text(encoding="utf-8"))

    def yc(*arguments):
        result = subprocess.run([args.yc, *arguments], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError("YC authorization unavailable")
        return result.stdout.strip()

    recipient = json.loads(yc("iam", "whoami", "--format", "json"))
    # A service account cannot silently become the notification recipient.
    user = json.loads(yc("iam", "user-account", "get", "--id", recipient, "--format", "json"))
    if user["id"] != recipient:
        raise ValueError("Expected an authenticated user")
    token = yc("iam", "create-token")

    def request(path, body=None):
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        data = None if body is None else json.dumps(body).encode()
        with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, headers=headers), timeout=30) as response:
            return json.load(response)

    print(json.dumps(configure(config, state, recipient, request, args.apply), indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        status = f" HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else ""
        print(f"Budget configuration failed ({type(error).__name__}{status}); credentials and response bodies are not logged.", file=sys.stderr)
        sys.exit(1)
