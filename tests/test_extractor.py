import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import extractor as ex
from fields import normalize, check, PARCEL_FIELDS


def result(parcels, address=""):
    return {"property": {"property_address": address}, "parcels": parcels}


def agency(name="", insts=(), aid="", details=""):
    return {"id": aid, "agency_name": name, "agency_details": details,
            "installments": [{"id": "", "tax_amount": a, "tax_due_date": d, "tax_note": n} for a, d, n in insts]}


def parcel(number="", addr="", insts=(), pid="", agencies=None, agency_name="County Tax Office"):
    if agencies is None:
        agencies = [agency(agency_name, insts)] if insts else []
    return {"id": pid, "parcel_number": number, "parcel_address": addr, "agencies": agencies}


def test_normalize():
    assert normalize("amount", "$1,250") == "1250.00"
    assert normalize("amount", "about lots") == "about lots"
    assert normalize("date", "03/15/2026") == "2026-03-15"
    assert normalize("date", "March 15, 2026") == "2026-03-15"
    assert normalize("string", "  x ") == "x"


def test_check_flags_but_does_not_reject():
    assert check({"type": "date"}, "next week")
    assert not check({"type": "date"}, "2026-03-15")
    assert check({"type": "amount"}, "abc")
    assert check({"type": "string", "pattern": r"\d{3}-\d{3}"}, "12-3")


def test_multiple_parcels_and_installments():
    s = ex.new_state()
    ex.merge(s, result([parcel("111-222", "1 Oak St", [("500", "2026-04-10", "1st half"), ("500", "2026-10-10", "2nd half")]),
                        parcel("333-444", "N/A", [("90", "2026-04-10", "")])], "9 Elm Rd"), set(), set())
    assert s["property"]["property_address"] == "9 Elm Rd"
    assert [p["parcel_number"] for p in s["parcels"]] == ["111-222", "333-444"]
    assert len(s["parcels"][0]["agencies"][0]["installments"]) == 2
    assert s["parcels"][0]["agencies"][0]["installments"][0]["tax_amount"] == "500.00"


def test_update_by_id_and_by_parcel_number_no_duplicates():
    s = ex.new_state()
    ex.merge(s, result([parcel("111-222")]), set(), set())
    pid = s["parcels"][0]["id"]
    ex.merge(s, result([parcel("111222", "1 Oak St", pid=pid)]), set(), set())   # by id
    ex.merge(s, result([parcel("111-222", "1 Oak St")]), set(), set())          # by number
    assert len(s["parcels"]) == 1
    assert s["parcels"][0]["parcel_address"] == "1 Oak St"


def test_empty_never_clears_and_returns_unchanged():
    s = ex.new_state()
    ex.merge(s, result([parcel("111-222", "1 Oak St")]), set(), set())
    assert ex.merge(s, result([parcel("111-222", "")]), set(), set()) is False
    assert s["parcels"][0]["parcel_address"] == "1 Oak St"


def test_locked_field_is_not_overwritten():
    s = ex.new_state()
    ex.merge(s, result([parcel("111-222", "1 Oak St")]), set(), set())
    pid = s["parcels"][0]["id"]
    locked = {(pid, "parcel_address")}
    ex.merge(s, result([parcel("111-222", "WRONG")]), locked, set())
    assert s["parcels"][0]["parcel_address"] == "1 Oak St"


def test_deleted_parcel_and_installment_stay_deleted():
    s = ex.new_state()
    ex.merge(s, result([parcel("111-222", insts=[("500", "2026-04-10", "")])]), set(), set())
    p = s["parcels"][0]
    a = p["agencies"][0]
    deleted = {("inst", a["id"], "500.00", "2026-04-10")}
    a["installments"].clear()
    ex.merge(s, result([parcel("111-222", insts=[("500", "2026-04-10", "")])]), set(), deleted)
    assert a["installments"] == []
    deleted = {ex.agency_signature(p["id"], a)}
    p["agencies"].clear()
    ex.merge(s, result([parcel("111-222", insts=[("500", "2026-04-10", "")])]), set(), deleted)
    assert p["agencies"] == []
    s["parcels"].clear()
    deleted = {("parcel", "111222")}
    ex.merge(s, result([parcel("111-222")]), set(), deleted)
    assert s["parcels"] == []


def test_schema_follows_fields():
    schema = ex.build_schema()
    item = schema["properties"]["parcels"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])
    ag = item["properties"]["agencies"]["items"]
    assert "agency_name" in ag["properties"]
    assert "tax_amount" in ag["properties"]["installments"]["items"]["properties"]


def test_store_roundtrip_and_export(tmp_path, monkeypatch):
    import store
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "t.db"))
    s = ex.new_state()
    ex.merge(s, result([parcel("111-222", "1 Oak St", [("500", "2026-04-10", "a"), ("600", "2026-10-10", "b")]),
                        parcel("333-444")], "9 Elm Rd"), set(), set())
    store.save_call("c1", s, "hello")
    store.save_call("c1", s, "hello again")          # upsert, no duplicates
    import pandas as pd
    df = pd.read_excel(store.export_xlsx())
    assert len(df) == 3                              # 2 installments + 1 parcel with none
    assert set(df["Property Address"]) == {"9 Elm Rd"}


def test_multiple_agencies_per_parcel():
    s = ex.new_state()
    ex.merge(s, result([parcel("111-222", agencies=[
        agency("County", [("500", "2026-04-10", "")]),
        agency("School District", [("300", "2026-05-01", "")])])]), set(), set())
    ex.merge(s, result([parcel("111-222", agencies=[agency("county", [("500", "2026-04-10", "paid")])])]), set(), set())
    ags = s["parcels"][0]["agencies"]
    assert [a["agency_name"].lower() for a in ags] == ["county", "school district"]
    assert ags[0]["installments"][0]["tax_note"] == "paid" and len(ags[0]["installments"]) == 1


def test_repeat_does_not_duplicate_rewrite_or_clear():
    s = ex.new_state()
    ex.merge(s, result([parcel("111-222", "1 Oak St", [("500", "2026-04-10", "1st half")])], "9 Elm Rd"), set(), set())
    before = json.dumps(s, sort_keys=True)
    # read-back: different wording, one value garbled, some fields blank
    ex.merge(s, result([parcel("111222", "", [("$500.00", "04/10/2026", "")])], "9 Elm"), set(), set())
    ex.merge(s, result([parcel("111-222", "1 Oak Street Apt", [("5000", "2026-04-10", "")])], ""), set(), set())
    after = json.loads(before)
    # only extension of a text value is allowed (address got longer); amount was not rewritten
    assert len(s["parcels"]) == 1 and len(s["parcels"][0]["agencies"]) == 1
    assert len(s["parcels"][0]["agencies"][0]["installments"]) == 1
    assert s["parcels"][0]["agencies"][0]["installments"][0]["tax_amount"] == "500.00"
    assert s["parcels"][0]["agencies"][0]["installments"][0]["tax_note"] == "1st half"
    assert s["property"]["property_address"] == "9 Elm Rd"
    assert after["parcels"][0]["parcel_number"] == s["parcels"][0]["parcel_number"]


def test_agency_details_extend_but_never_shrink():
    s = ex.new_state()
    ex.merge(s, result([parcel("1", agencies=[agency("County Tax Office", details="Phone 555-0100")])]), set(), set())
    ex.merge(s, result([parcel("1", agencies=[agency("County", details="Phone 555-0100; www.county.gov")])]), set(), set())
    ex.merge(s, result([parcel("1", agencies=[agency("County Tax Office", details="")])]), set(), set())
    ags = s["parcels"][0]["agencies"]
    assert len(ags) == 1 and ags[0]["agency_name"] == "County Tax Office"
    assert ags[0]["agency_details"] == "Phone 555-0100; www.county.gov"


def test_explicit_correction_replaces_value_but_respects_lock():
    s = ex.new_state()
    ex.merge(s, result([parcel("1", insts=[("500", "2026-04-10", "")])]), set(), set())
    iid = s["parcels"][0]["agencies"][0]["installments"][0]["id"]
    r = result([]); r["corrections"] = [{"id": iid, "key": "tax_amount", "value": "$550", "evidence": "no, 550"}]
    assert ex.merge(s, r, set(), set())
    assert s["parcels"][0]["agencies"][0]["installments"][0]["tax_amount"] == "550.00"
    r["corrections"][0]["value"] = "999"
    ex.merge(s, r, {(iid, "tax_amount")}, set())
    assert s["parcels"][0]["agencies"][0]["installments"][0]["tax_amount"] == "550.00"


def test_date_filled_later_matches_same_amount_installment():
    s = ex.new_state()
    ex.merge(s, result([parcel("1", insts=[("500", "", "")])]), set(), set())
    ex.merge(s, result([parcel("1", insts=[("500", "2026-04-10", "")])]), set(), set())
    insts = s["parcels"][0]["agencies"][0]["installments"]
    assert len(insts) == 1 and insts[0]["tax_due_date"] == "2026-04-10"


def test_agencies_csv_one_row_per_agency_no_duplicates(tmp_path, monkeypatch):
    import store, csv
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "t.db"))
    s = ex.new_state()
    ex.merge(s, result([parcel("111-222", "1 Oak St", agencies=[agency("County", [("500", "2026-04-10", "")], details="Phone 555"),
                                                                  agency("School", [])]),
                        parcel("333-444")], "9 Elm Rd"), set(), set())
    store.save_call("c1", s, "t"); store.save_call("c1", s, "t again")
    rows = list(csv.DictReader(open(store.csv_path(), encoding="utf-8-sig")))
    assert len(rows) == 2                                   # parcel without agency gives no row; installments not repeated
    assert rows[0]["Agency Name"] == "County" and rows[0]["Agency Details"] == "Phone 555"
    assert rows[0]["Property Address"] == "9 Elm Rd" and rows[0]["Parcel Number"] == "111-222"
    assert rows[0]["Parcel Address"] == "1 Oak St"
