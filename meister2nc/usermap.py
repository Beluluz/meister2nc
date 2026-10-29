"""Map MeisterTask persons to Nextcloud user ids by e-mail.

Sources: every ``persons.json``/``me.json``/project member list in the raw
dumps, Nextcloud's ``/cloud/users/details`` (admin or sub-admin), and manual
overrides in ``data/usermap.overrides.json``::

    {"12345678": "alice", "email:someone@example.org": "someone"}

Result: ``state/usermap.json`` with one entry per person id.
"""

from __future__ import annotations

from .config import Config
from .extract_tasks import collect_persons
from .nc_client import NCClient
from .util import log, read_json, write_json


def person_name(p: dict | None) -> str:
    if not p:
        return "unbekannt"
    return " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x).strip() or p.get("email") or str(p.get("id"))


def email_variants(mail: str | None) -> list[str]:
    """Exact address first, then the address without a ``+tag`` (Meister marks
    closed accounts as ``first.last+closed@domain``; Nextcloud has no tag)."""
    mail = (mail or "").strip().lower()
    if not mail or "@" not in mail:
        return []
    local, domain = mail.split("@", 1)
    variants = [mail]
    if "+" in local:
        variants.append(local.split("+", 1)[0] + "@" + domain)
    return variants


def meister_closed(mail: str | None) -> bool:
    mail = (mail or "").strip().lower()
    return "+closed@" in mail


def build(cfg: Config, nc: NCClient | None) -> dict:
    persons = collect_persons(cfg)
    overrides = read_json(cfg.data_dir / "usermap.overrides.json", {}) or {}
    email_index: dict[str, str] = {}
    enabled: dict[str, bool] = {}
    nc_users = None
    if nc is not None:
        try:
            nc_users = nc.users_details(include_disabled=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not list Nextcloud users: %s", exc)
        if nc_users:
            for uid, info in nc_users.items():
                if not isinstance(info, dict):
                    continue
                enabled[uid] = bool(info.get("enabled", True))
                for mail in [info.get("email")] + list(info.get("additional_mail") or []):
                    if mail:
                        # an enabled account wins over a disabled one with the same address
                        key = mail.strip().lower()
                        if key not in email_index or (enabled[uid] and not enabled.get(email_index[key], True)):
                            email_index[key] = uid
        else:
            log.warning("Nextcloud user list unavailable (not admin?) – relying on usermap.overrides.json")
    result = {"persons": {}, "unmapped": [], "disabled": [], "nc_users": len(nc_users or {}),
              "nc_users_disabled": sum(1 for v in enabled.values() if not v)}
    def resolve_override(value: str | None) -> str | None:
        """Override targets are Nextcloud user ids, or ``email:<address>`` which
        is looked up in the Nextcloud user listing."""
        if not value:
            return None
        if value.startswith("email:"):
            target = email_index.get(value[6:].strip().lower())
            if not target:
                log.warning("override target %s not found among Nextcloud users", value)
            return target
        return value

    result["closed_in_meister"] = []
    for pid, p in sorted(persons.items()):
        variants = email_variants(p.get("email"))
        uid = resolve_override(overrides.get(str(pid)))
        matched = "override" if uid else None
        for i, mail in enumerate(variants):
            if uid:
                break
            if overrides.get(f"email:{mail}"):
                uid, matched = resolve_override(overrides[f"email:{mail}"]), "override"
            elif mail in email_index:
                uid, matched = email_index[mail], "email" if i == 0 else "email-without-tag"
        closed = meister_closed(p.get("email"))
        entry = {"name": person_name(p), "email": p.get("email"), "uid": uid, "matched_by": matched,
                 "enabled": enabled.get(uid, True) if uid else None, "meister_closed": closed,
                 "groups": list(((nc_users or {}).get(uid) or {}).get("groups") or []) if uid else []}
        result["persons"][str(pid)] = entry
        if not uid:
            result["unmapped"].append({"id": pid, **entry})
        elif entry["enabled"] is False:
            result["disabled"].append({"id": pid, **entry})
        if closed:
            result["closed_in_meister"].append({"id": pid, **entry})
    write_json(cfg.state_dir / "usermap.json", result)
    log.info("usermap: %d persons, %d mapped (%d to disabled accounts), %d unmapped", len(result["persons"]),
             len(result["persons"]) - len(result["unmapped"]), len(result["disabled"]), len(result["unmapped"]))
    for u in result["unmapped"]:
        log.info("  unmapped: %s <%s> (id %s)", u["name"], u["email"], u["id"])
    for u in result["disabled"]:
        log.info("  disabled account: %s <%s> → %s (comments attributed, no assignments)", u["name"], u["email"], u["uid"])
    for u in result["closed_in_meister"]:
        log.info("  closed in Meister: %s <%s> → %s", u["name"], u["email"], u["uid"] or "no Nextcloud account")
    return result


class UserMap:
    def __init__(self, cfg: Config):
        data = read_json(cfg.state_dir / "usermap.json", {}) or {}
        self.persons: dict[str, dict] = data.get("persons", {})
        self.extra_names: dict[str, str] = {}

    def uid(self, person_id) -> str | None:
        """Nextcloud uid for a person, enabled or disabled (for authorship)."""
        if person_id is None:
            return None
        return (self.persons.get(str(person_id)) or {}).get("uid")

    def enabled(self, person_id) -> bool:
        """Mapped, Nextcloud account enabled, and not closed on the Meister side."""
        entry = self.persons.get(str(person_id)) or {}
        return bool(entry.get("uid")) and entry.get("enabled") is not False and not entry.get("meister_closed")

    def uid_active(self, person_id) -> str | None:
        """Nextcloud uid only when the account is enabled (for assignments/ACL)."""
        return self.uid(person_id) if self.enabled(person_id) else None

    def groups_of(self, uid: str) -> set[str]:
        """Nextcloud groups of a mapped user (as listed by the admin API at usermap time)."""
        for entry in self.persons.values():
            if entry.get("uid") == uid:
                return set(entry.get("groups") or [])
        return set()

    def name(self, person_id) -> str:
        if person_id is None:
            return "unbekannt"
        entry = self.persons.get(str(person_id))
        if entry:
            return entry["name"]
        return self.extra_names.get(str(person_id)) or f"Ehemaliges Mitglied (MeisterTask-ID {person_id})"
