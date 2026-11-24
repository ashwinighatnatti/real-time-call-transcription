"""Single source of truth for what gets extracted.

To add or change a field, edit the lists below. The extraction prompt, the JSON
schema, the web form, the database columns and the Excel export all follow.

Structure:  property  ->  parcels[]  ->  agencies[]  ->  installments[]
"""
import os
import re
from datetime import datetime

# type is one of: "string", "amount", "date"
PROPERTY_FIELDS = [
    {"key": "property_address", "label": "Property Address", "type": "string",
     "hint": "The address as the caller gives it. May be informal, or a mailing address such as a P.O. Box."},
]

PARCEL_FIELDS = [
    {"key": "parcel_number", "label": "Parcel Number", "type": "string",
     "hint": "Assessor's Parcel Number (APN). Join digits spoken one by one; keep dashes if spoken.",
     # Optional regex to flag (not reject) unexpected formats, e.g. r"\d{3}-\d{3}-\d{3}"
     "pattern": os.environ.get("PARCEL_NUMBER_PATTERN")},
    {"key": "parcel_address", "label": "Parcel Address", "type": "string",
     "hint": "Official situs address of the land from the county record, only if someone states it as such. "
             "Never copy the property address here. Vacant land may be '0 Main St' or 'N/A'."},
]

AGENCY_FIELDS = [
    {"key": "agency_name", "label": "Agency Name", "type": "string",
     "hint": "The taxing agency or collector for this parcel, e.g. 'Travis County Tax Office'."},
    {"key": "agency_details", "label": "Agency Details", "type": "string",
     "hint": "Anything stated about the agency besides its name: phone, address, website, hours, "
             "account or payment info. One short line, items separated by '; ', "
             "e.g. 'Phone 555-010-2000; 100 Main St, Austin TX; www.example.gov'. Empty if none was stated."},
]

INSTALLMENT_FIELDS = [
    {"key": "tax_amount", "label": "Tax Amount", "type": "amount",
     "hint": "Digits only, e.g. 1250.00."},
    {"key": "tax_due_date", "label": "Tax Due Date", "type": "date",
     "hint": "ISO format YYYY-MM-DD. Use the current year if the year is not said."},
    {"key": "tax_note", "label": "Installment Note", "type": "string",
     "hint": "Short label if stated, e.g. '1st half', '2nd half', 'delinquent', 'current'."},
]

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y")


def normalize(ftype, value):
    """Clean a raw model value. Never raises; returns the raw text if it can't parse."""
    v = (value or "").strip()
    if not v:
        return ""
    if ftype == "amount":
        digits = re.sub(r"[^\d.]", "", v)
        try:
            return f"{float(digits):.2f}"
        except ValueError:
            return v
    if ftype == "date":
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return v


def check(field, value):
    """Return a warning message for a suspicious value, or '' if fine. Flags, never rejects."""
    if not value:
        return ""
    if field["type"] == "amount":
        try:
            float(value)
        except ValueError:
            return "Not a valid amount"
    if field["type"] == "date":
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return "Not a valid date (expected YYYY-MM-DD)"
    pattern = field.get("pattern")
    if pattern and not re.fullmatch(pattern, value):
        return "Unexpected format"
    return ""
