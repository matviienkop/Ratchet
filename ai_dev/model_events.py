"""Record role-model availability without copying provider payloads into the card."""
from .store import record

REASONS = {
    "billing": "провайдер сообщил об исчерпании средств или квоты",
    "rate_limit": "достигнут лимит запросов или квоты",
    "upstream_rate_limit": "достигнут лимит вышестоящего провайдера",
    "overloaded": "модель перегружена", "server_error": "сбой сервиса",
    "timeout": "провайдер не ответил вовремя", "model_not_found": "модель недоступна",
    "model_entitlement": "модель недоступна по подписке", "auth": "ошибка авторизации",
    "auth_permanent": "провайдер отклонил авторизацию",
}


def failure(card, lane, model, provider, session, reason, status):
    if not lane or reason not in REASONS:
        return
    row = dict(model=model, provider=provider, session=session, reason=reason, http=status)
    with card.locked() as state:
        failures = state.setdefault("model_errors", {})
        if failures.get(lane) != row:
            failures[lane] = row
            record(state, "model.unavailable", lane=lane, **row)


def attempt(card, lane, model, provider, session):
    if not lane:
        return
    route = dict(model=model, provider=provider, session=session)
    if card.load().get("observed_routes", {}).get(lane) == route:
        return
    with card.locked() as state:
        routes = state.setdefault("observed_routes", {})
        if routes.get(lane) == route:
            return
        old = state.setdefault("model_errors", {}).pop(lane, None)
        routes[lane] = route
        record(state, "model.attempt", lane=lane, **route)
        if old and old["provider"] != provider and old["session"] == session:
            text = (f"{lane}: {old['model']} ({old['provider']}) недоступна — {REASONS[old['reason']]}. "
                    f"Переключаюсь на {model} ({provider}).")
            record(state, "model.switch", lane=lane, previous=old, current=route, text=text,
                   delivery={"status": "pending", "id": f"m{len(state['log']) + 1}"})
