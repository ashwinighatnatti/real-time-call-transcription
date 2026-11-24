"""LLM extraction + merge logic. No audio, web or storage code in here."""
import json
import os
import re
import uuid

from fields import (PROPERTY_FIELDS, PARCEL_FIELDS, AGENCY_FIELDS, INSTALLMENT_FIELDS,
                    normalize, check)

TOOL_NAME = "record_extraction"
_client = None


def _get_client():
    global _client
    if _client is None:
        from anthropic import AnthropicFoundry
        _client = AnthropicFoundry(
            api_key=os.environ["AZURE_ANTHROPIC_API_KEY"],
            base_url=os.environ["AZURE_ANTHROPIC_API_ENDPOINT"],
        )
    return _client


# ---------------------------------------------------------------- state
def _new_id():
    return uuid.uuid4().hex[:6]


def new_installment():
    return {"id": _new_id(), **{f["key"]: "" for f in INSTALLMENT_FIELDS}}


def new_agency():
    return {"id": _new_id(), **{f["key"]: "" for f in AGENCY_FIELDS}, "installments": []}


def new_parcel():
    return {"id": _new_id(), **{f["key"]: "" for f in PARCEL_FIELDS}, "agencies": []}


def new_state():
    return {"property": {f["key"]: "" for f in PROPERTY_FIELDS}, "parcels": []}


def public_state(state):
    """Copy of the state plus per-field warnings, for the UI."""
    warnings = {}
    for f in PROPERTY_FIELDS:
        w = check(f, state["property"][f["key"]])
        if w:
            warnings[f"property.{f['key']}"] = w
    for p in state["parcels"]:
        for f in PARCEL_FIELDS:
            w = check(f, p[f["key"]])
            if w:
                warnings[f"{p['id']}.{f['key']}"] = w
        for a in p["agencies"]:
            for f in AGENCY_FIELDS:
                w = check(f, a[f["key"]])
                if w:
                    warnings[f"{a['id']}.{f['key']}"] = w
            for i in a["installments"]:
                for f in INSTALLMENT_FIELDS:
                    w = check(f, i[f["key"]])
                    if w:
                        warnings[f"{i['id']}.{f['key']}"] = w
    return {"property": dict(state["property"]),
            "parcels": json.loads(json.dumps(state["parcels"])),
            "warnings": warnings}


# ---------------------------------------------------------------- schema + prompt
def _str_props(fields):
    return {f["key"]: {"type": "string"} for f in fields}


def build_schema():
    installment = {
        "type": "object",
        "properties": {"id": {"type": "string"}, **_str_props(INSTALLMENT_FIELDS)},
        "required": ["id"] + [f["key"] for f in INSTALLMENT_FIELDS],
        "additionalProperties": False,
    }
    agency = {
        "type": "object",
        "properties": {"id": {"type": "string"}, **_str_props(AGENCY_FIELDS),
                       "installments": {"type": "array", "items": installment}},
        "required": ["id"] + [f["key"] for f in AGENCY_FIELDS] + ["installments"],
        "additionalProperties": False,
    }
    parcel = {
        "type": "object",
        "properties": {"id": {"type": "string"}, **_str_props(PARCEL_FIELDS),
                       "agencies": {"type": "array", "items": agency}},
        "required": ["id"] + [f["key"] for f in PARCEL_FIELDS] + ["agencies"],
        "additionalProperties": False,
    }
    prop = {
        "type": "object",
        "properties": _str_props(PROPERTY_FIELDS),
        "required": [f["key"] for f in PROPERTY_FIELDS],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "property": prop,
            "parcels": {"type": "array", "items": parcel},
            "corrections": {"type": "array", "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "key": {"type": "string"},
                               "value": {"type": "string"}, "evidence": {"type": "string"}},
                "required": ["id", "key", "value", "evidence"],
                "additionalProperties": False}},
        },
        "required": ["reasoning", "property", "parcels", "corrections"],
        "additionalProperties": False,
    }
    return schema


def _lines(fields):
    return "\n".join(f"- {f['key']}: {f['hint']}" for f in fields)


def build_system_prompt():
    return (
        "You extract property tax details from a live phone-call transcript.\n"
        "A property can have several parcels; each parcel can have several taxing agencies; each agency can have several tax installments.\n"
        "Rules:\n"
        "- Only use what is explicitly said. Use an empty string for anything not stated. Never guess.\n"
        "- You are given CURRENT STATE. Return every parcel, agency and installment already in it, "
        "keeping its id and its current values unchanged, and add new ones with id set to an empty string.\n"
        "- Do not move a value between parcels or installments unless the speaker clearly does.\n"
        "- REPEATS AND CONFIRMATIONS: speakers often repeat or read back details ('so that is parcel 123-456-789, "
        "correct?', 'yes, 1,250 due April 10'). A repeat or confirmation of something already in CURRENT STATE "
        "is NOT new information: do not create another parcel, agency or installment, and do not change the "
        "stored value. Re-spelled or re-formatted versions of the same value ('one two three' vs '123') are repeats.\n"
        "- NEVER blank out or drop anything already in CURRENT STATE.\n"
        "- ADDITIONS: if a repeat adds a new detail (e.g. the agency's phone is now given), return the stored text "
        "plus the new detail. Never return less than what is stored.\n"
        "- CORRECTIONS: only when a speaker explicitly says an earlier value was wrong ('no, it is 4 5 6, not 4 5 7', "
        "'actually the due date is May 1') add an entry to corrections with the item id, the field key, the new "
        "value and the quote as evidence. Use id 'property' for property fields. A different value that is merely "
        "a mishearing or unclear is not a correction.\n"
        "- First write a brief reasoning: which items are new, which are repeats/confirmations you ignored, "
        "and which are corrections.\n\n"
        f"Property fields:\n{_lines(PROPERTY_FIELDS)}\n\n"
        f"Parcel fields:\n{_lines(PARCEL_FIELDS)}\n\n"
        f"Agency fields (each parcel has a list of agencies):\n{_lines(AGENCY_FIELDS)}\n\n"
        f"Installment fields (each agency has a list):\n{_lines(INSTALLMENT_FIELDS)}"
    )


def extract(state_snapshot, transcript, deployment):
    """One LLM call. state_snapshot is public_state() output; returns the raw schema dict.
    The model is forced to answer through a single tool whose input schema is our schema."""
    current = {"property": state_snapshot["property"], "parcels": state_snapshot["parcels"]}
    resp = _get_client().messages.create(
        model=deployment,                      # on Azure this is the deployment name
        max_tokens=2000,
        system=build_system_prompt(),
        messages=[{"role": "user",
                   "content": f"CURRENT STATE:\n{json.dumps(current)}\n\nTRANSCRIPT:\n{transcript}"}],
        tools=[{"name": TOOL_NAME, "description": "Record the extracted call details.",
                "input_schema": build_schema()}],
        tool_choice={"type": "tool", "name": TOOL_NAME},
    )
    for block in resp.content:
        if block.type == "tool_use":
            if block.input.get("reasoning"):
                print("[extract]", block.input["reasoning"][:300])
            return block.input
    raise RuntimeError("model returned no tool call")


# ---------------------------------------------------------------- merge
def _key(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _find(items, item_id, match):
    """Match by id first, then by a fallback predicate; None if neither hits."""
    for it in items:
        if item_id and it["id"] == item_id:
            return it
    for it in items:
        if match(it):
            return it
    return None


_TYPES = {f["key"]: f["type"] for f in PROPERTY_FIELDS + PARCEL_FIELDS + AGENCY_FIELDS + INSTALLMENT_FIELDS}


def _accept(ftype, old, new):
    """Whether a re-stated value may replace the stored one. Stored values are never rewritten
    by a repeat. Only an empty slot is filled, or a text value is extended with more detail."""
    if not new or old == new:
        return False
    if not old:
        return True
    return ftype == "string" and len(_key(new)) > len(_key(old)) and _key(old) in _key(new)


def _apply(target, values, locked):
    changed = False
    for k, v in values.items():
        if (target["id"], k) not in locked and _accept(_TYPES.get(k, "string"), target[k], v):
            target[k] = v
            changed = True
    return changed


def _same_name(a, b):
    """Agency names match if equal, or one contains the other ('County' / 'County Tax Office')."""
    a, b = _key(a), _key(b)
    return bool(a and b and (a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a))))


def _apply_corrections(state, corrections, locked):
    """Explicit speaker corrections are the only way a stored value is replaced."""
    items = {"property": state["property"]}
    for p in state["parcels"]:
        items[p["id"]] = p
        for a in p["agencies"]:
            items[a["id"]] = a
            for i in a["installments"]:
                items[i["id"]] = i
    changed = False
    for c in corrections or []:
        target, k = items.get(c.get("id", "")), c.get("key", "")
        if target is None or k not in target or isinstance(target[k], list) or k == "id":
            continue
        if (c["id"], k) in locked:
            continue
        v = normalize(_TYPES.get(k, "string"), c.get("value", ""))
        if v and target[k] != v:
            target[k] = v
            changed = True
    return changed


def installment_signature(agency_id, inst):
    return ("inst", agency_id, inst["tax_amount"], inst["tax_due_date"])


def agency_signature(parcel_id, agency):
    return ("agency", parcel_id, _key(agency["agency_name"]))


def merge(state, result, locked, deleted):
    """Fold an extraction result into state. Rules: empty values never clear a field,
    user-edited (locked) fields are never overwritten, deleted items stay deleted.
    Returns True if anything changed."""
    changed = False

    for f in PROPERTY_FIELDS:
        v = normalize(f["type"], result.get("property", {}).get(f["key"], ""))
        if ("property", f["key"]) not in locked and _accept(f["type"], state["property"][f["key"]], v):
            state["property"][f["key"]] = v
            changed = True

    for item in result.get("parcels", []):
        pvals = {f["key"]: normalize(f["type"], item.get(f["key"], "")) for f in PARCEL_FIELDS}
        agencies = []
        for araw in item.get("agencies", []):
            avals = {f["key"]: normalize(f["type"], araw.get(f["key"], "")) for f in AGENCY_FIELDS}
            insts = []
            for raw in araw.get("installments", []):
                ivals = {f["key"]: normalize(f["type"], raw.get(f["key"], "")) for f in INSTALLMENT_FIELDS}
                if any(ivals.values()):
                    insts.append((raw.get("id", ""), ivals))
            if any(avals.values()) or insts:
                agencies.append((araw.get("id", ""), avals, insts))
        if not any(pvals.values()) and not agencies:
            continue

        pn = _key(pvals["parcel_number"])
        parcel = _find(state["parcels"], item.get("id", ""),
                       lambda p: pn and _key(p["parcel_number"]) == pn)
        if parcel is None:
            if pn and ("parcel", pn) in deleted:
                continue
            parcel = new_parcel()
            state["parcels"].append(parcel)
            changed = True
        changed |= _apply(parcel, pvals, locked)

        for agency_id, avals, insts in agencies:
            an = _key(avals["agency_name"])
            agency = _find(parcel["agencies"], agency_id,
                           lambda a: _same_name(a["agency_name"], avals["agency_name"]))
            if agency is None:
                if an and agency_signature(parcel["id"], avals) in deleted:
                    continue
                agency = new_agency()
                parcel["agencies"].append(agency)
                changed = True
            changed |= _apply(agency, avals, locked)

            for inst_id, ivals in insts:
                inst = _find(agency["installments"], inst_id,
                             lambda i: (ivals["tax_due_date"] and i["tax_due_date"] == ivals["tax_due_date"])
                             or (ivals["tax_amount"] and i["tax_amount"] == ivals["tax_amount"]
                                 and (not ivals["tax_due_date"] or not i["tax_due_date"])))
                if inst is None:
                    probe = {"tax_amount": ivals["tax_amount"], "tax_due_date": ivals["tax_due_date"]}
                    if installment_signature(agency["id"], probe) in deleted:
                        continue
                    inst = new_installment()
                    agency["installments"].append(inst)
                    changed = True
                changed |= _apply(inst, ivals, locked)

    changed |= _apply_corrections(state, result.get("corrections"), locked)
    return changed
