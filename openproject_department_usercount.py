#!/usr/bin/env python3
"""
Zaehlt eindeutige aktive Nutzer mit Projektzugriff je Abteilung (Community-Variante).

Konfiguration erfolgt ueber eine properties-Datei (Java-Stil, key=value).
Standardpfad: config.properties im Verzeichnis dieses Skripts; alternativ als
zweites Argument beim Aufruf uebergeben.

Erwartete Schluessel:
    op.base.url        Pflicht   z. B. https://openproject.example.com
    op.api.key         Pflicht   API-Token eines Nutzers mit Admin-/manage_user-Rechten
    op.api.user        optional  Basic-Auth-Benutzername (Default: apikey)
    op.department.attr optional  Name des Projektattributs (Default: Abteilung)
    op.proxy.url       optional  lokaler Winfoom-Proxy (Default: http://localhost:3129);
                                 leer lassen (op.proxy.url=) um ohne Proxy zu arbeiten

Voraussetzungen:
    pip install requests

Aufruf:
    python openproject_department_usercount.py "Vertrieb"
    python openproject_department_usercount.py "Vertrieb" /pfad/zu/config.properties
"""

import os
import sys
import json

import requests

PAGE_SIZE = 100

# Werden in configure() aus der properties-Datei befuellt:
session = requests.Session()
BASE_URL = ""


def load_properties(path):
    """Liest eine einfache Java-style properties-Datei (key=value bzw. key:value).

    Unterstuetzt Kommentarzeilen (# oder !), Leerzeilen und beide Trennzeichen.
    """
    props = {}
    with open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line[0] in "#!":
                continue
            positions = [line.index(sep) for sep in "=:" if sep in line]
            if not positions:
                continue
            cut = min(positions)
            key = line[:cut].strip()
            value = line[cut + 1:].strip()
            props[key] = value
    return props


def api_get(path, filters=None, params=None):
    """Einzelner GET-Aufruf gegen die API v3.

    Bei Fehlern wird die OpenProject-Fehlermeldung (errorIdentifier/message)
    mit in die Exception aufgenommen, damit z. B. ein 401 sofort erklaert ist.
    """
    params = dict(params or {})
    if filters is not None:
        params["filters"] = json.dumps(filters, separators=(",", ":"))
    resp = session.get(f"{BASE_URL}/api/v3{path}", params=params, timeout=30)
    if not resp.ok:
        try:
            body = resp.json()
            detail = f" | {body.get('errorIdentifier', '')}: {body.get('message', '')}"
        except ValueError:
            detail = f" | {resp.text[:200]}"
        raise requests.HTTPError(f"{resp.status_code} {resp.reason} bei {path}{detail}", response=resp)
    return resp.json()


def api_collection(path, filters=None):
    """Paginiert eine HAL-Collection und liefert alle Elemente einzeln zurueck."""
    offset = 1
    while True:
        data = api_get(path, filters=filters, params={"offset": offset, "pageSize": PAGE_SIZE})
        elements = data.get("_embedded", {}).get("elements", [])
        for element in elements:
            yield element
        total = data.get("total", 0)
        if not elements or offset * PAGE_SIZE >= total:
            break
        offset += 1


def fetch_project_schema():
    """Holt das Projekt-Schema.

    In neueren Versionen heissen Projekte 'workspaces'; /projects/schema ist
    veraltet. Daher zuerst /workspaces/schema versuchen, dann als Fallback
    /projects/schema.
    """
    for schema_path in ("/workspaces/schema", "/projects/schema"):
        try:
            return api_get(schema_path)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 410):            # nicht vorhanden / veraltet -> naechsten Pfad testen
                continue
            raise                                # 401/403 etc. unveraendert weiterreichen
    raise SystemExit("Projekt-Schema nicht erreichbar (weder /workspaces/schema noch /projects/schema).")


def resolve_department_field_key(attribute_name):
    """Findet den customFieldN-Schluessel, dessen Name dem Abteilungs-Attribut entspricht."""
    schema = fetch_project_schema()
    for key, spec in schema.items():
        if key.startswith("customField") and isinstance(spec, dict) and spec.get("name") == attribute_name:
            return key
    raise SystemExit(f"Projektattribut '{attribute_name}' nicht im Projekt-Schema gefunden.")


def project_department(project, field_key):
    """Liest den Abteilungswert eines Projekts.

    Single-Select-Liste -> Wert steht als 'title' im _links-Block.
    Textfeld            -> Wert steht direkt als Property.
    (Multi-Select waere eine Liste unter _links -> bei Bedarf hier ergaenzen.)
    """
    link = project.get("_links", {}).get(field_key)
    if isinstance(link, dict):
        return link.get("title")
    return project.get(field_key)


def active_user_ids():
    """Menge aller aktiven Nutzer-IDs; dient als zentrale Status-Pruefung."""
    flt = [{"status": {"operator": "=", "values": ["active"]}}]
    return {user["id"] for user in api_collection("/users", filters=flt)}


def id_from_href(href):
    """Extrahiert die numerische ID aus einem HAL-Link wie '/api/v3/users/42'."""
    return int(href.rstrip("/").split("/")[-1])


def group_active_member_ids(group_id, active_ids, cache):
    """Aktive Mitglieder einer Gruppe (mit Cache), eingeschraenkt auf aktive Nutzer."""
    if group_id in cache:
        return cache[group_id]
    flt = [
        {"group": {"operator": "=", "values": [str(group_id)]}},
        {"status": {"operator": "=", "values": ["active"]}},
    ]
    members = {u["id"] for u in api_collection("/users", filters=flt) if u["id"] in active_ids}
    cache[group_id] = members
    return members


def count_users_for_department(department, field_key, active_ids):
    """Eindeutige aktive Nutzer mit Zugriff auf die Projekte einer Abteilung."""
    result = set()
    group_cache = {}

    # 1. Projekte der Abteilung bestimmen (clientseitige Filterung -> Community-tauglich)
    project_ids = [
        p["id"] for p in api_collection("/projects")
        if project_department(p, field_key) == department
    ]

    # 2. Mitgliedschaften je Projekt auswerten
    for pid in project_ids:
        flt = [{"project": {"operator": "=", "values": [str(pid)]}}]
        for membership in api_collection("/memberships", filters=flt):
            href = membership.get("_links", {}).get("principal", {}).get("href", "")
            if "/users/" in href:                       # direkt zugeordneter Nutzer
                uid = id_from_href(href)
                if uid in active_ids:
                    result.add(uid)
            elif "/groups/" in href:                    # Gruppe -> auf aktive Mitglieder aufloesen
                result |= group_active_member_ids(id_from_href(href), active_ids, group_cache)
            # '/placeholder_users/' wird bewusst uebersprungen (keine abrechenbaren Nutzer)

    return project_ids, result


def configure(config_path):
    """Laedt die properties-Datei und richtet Session, BASE_URL und Proxy ein.

    Gibt den Namen des Abteilungs-Attributs zurueck.
    """
    global BASE_URL
    if not os.path.isfile(config_path):
        raise SystemExit(f"Konfigurationsdatei nicht gefunden: {config_path}")
    config = load_properties(config_path)

    BASE_URL = config.get("op.base.url", "").rstrip("/")
    api_key = config.get("op.api.key", "")
    api_user = config.get("op.api.user", "apikey")
    attribute_name = config.get("op.department.attr", "Abteilung")
    proxy_url = config.get("op.proxy.url", "http://localhost:3129")

    missing = [k for k, v in (("op.base.url", BASE_URL), ("op.api.key", api_key)) if not v]
    if missing:
        raise SystemExit("Pflicht-Eintraege fehlen in der properties-Datei: " + ", ".join(missing))

    session.auth = (api_user, api_key)                  # HTTP Basic: Benutzer (i. d. R. 'apikey'), Passwort = Token
    session.headers.update({"Accept": "application/hal+json"})
    if proxy_url:
        # Winfoom stellt lokal einen unauthentifizierten Facade-Proxy bereit und
        # uebernimmt selbst die Anmeldung am Corporate-Proxy. Auch fuer HTTPS-Ziele
        # ist das Schema http://, da der Proxy per CONNECT durchtunnelt.
        session.proxies = {"http": proxy_url, "https": proxy_url}
        # Interne Hosts (z. B. eine On-Prem-Instanz) ggf. ueber NO_PROXY ausnehmen:
        #   export NO_PROXY="openproject.intern,10.0.0.0/8"

    return attribute_name


def main():
    if len(sys.argv) < 2:
        print(f"Aufruf: {sys.argv[0]} <Abteilungsname> [pfad/zu/config.properties]")
        sys.exit(1)

    department = sys.argv[1]
    default_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.properties")
    config_path = sys.argv[2] if len(sys.argv) > 2 else default_config

    attribute_name = configure(config_path)
    field_key = resolve_department_field_key(attribute_name)
    active_ids = active_user_ids()
    project_ids, users = count_users_for_department(department, field_key, active_ids)

    print(f"Abteilung:           {department}")
    print(f"Projekte:            {len(project_ids)} -> {project_ids}")
    print(f"Abrechenbare Nutzer: {len(users)}")

    # Anschluss an die Billing-DB (siehe frueheres Datenmodell), z. B.:
    # from datetime import date
    # save_snapshot(department=department, snapshot_date=date.today(), user_count=len(users))


if __name__ == "__main__":
    main()
