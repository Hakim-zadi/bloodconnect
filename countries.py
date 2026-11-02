"""
BloodConnect - countries and national-ID rules.

One place that knows:
  * every country a user can pick (ISO 3166-1 alpha-2 code + English name), and
  * which national identity document is asked for in each country
    (Pakistan -> CNIC, India -> Aadhaar, USA -> SSN, ...), plus how to validate it.

The same data is sent to the browser (see public_rules()) so the form and the
server always agree. The checksum functions below are mirrored in index.html
(search for "ID_CHECKS") - keep both in sync if you change one.

Countries that are not listed in ID_RULES use GENERIC_RULE
("National ID / Passport number", 6-20 letters or digits). To give another
country its own document, just add one entry to ID_RULES.
"""
import re

# ---------------------------------------------------------------------------
# Country list
# ---------------------------------------------------------------------------
_COUNTRY_DATA = """
AF|Afghanistan
AL|Albania
DZ|Algeria
AD|Andorra
AO|Angola
AG|Antigua and Barbuda
AR|Argentina
AM|Armenia
AU|Australia
AT|Austria
AZ|Azerbaijan
BS|Bahamas
BH|Bahrain
BD|Bangladesh
BB|Barbados
BY|Belarus
BE|Belgium
BZ|Belize
BJ|Benin
BT|Bhutan
BO|Bolivia
BA|Bosnia and Herzegovina
BW|Botswana
BR|Brazil
BN|Brunei
BG|Bulgaria
BF|Burkina Faso
BI|Burundi
CV|Cabo Verde
KH|Cambodia
CM|Cameroon
CA|Canada
CF|Central African Republic
TD|Chad
CL|Chile
CN|China
CO|Colombia
KM|Comoros
CG|Congo (Republic)
CD|Congo (Democratic Republic)
CR|Costa Rica
CI|Côte d'Ivoire
HR|Croatia
CU|Cuba
CY|Cyprus
CZ|Czechia
DK|Denmark
DJ|Djibouti
DM|Dominica
DO|Dominican Republic
EC|Ecuador
EG|Egypt
SV|El Salvador
GQ|Equatorial Guinea
ER|Eritrea
EE|Estonia
SZ|Eswatini
ET|Ethiopia
FJ|Fiji
FI|Finland
FR|France
GA|Gabon
GM|Gambia
GE|Georgia
DE|Germany
GH|Ghana
GR|Greece
GD|Grenada
GT|Guatemala
GN|Guinea
GW|Guinea-Bissau
GY|Guyana
HT|Haiti
HN|Honduras
HK|Hong Kong
HU|Hungary
IS|Iceland
IN|India
ID|Indonesia
IR|Iran
IQ|Iraq
IE|Ireland
IT|Italy
JM|Jamaica
JP|Japan
JO|Jordan
KZ|Kazakhstan
KE|Kenya
KI|Kiribati
XK|Kosovo
KW|Kuwait
KG|Kyrgyzstan
LA|Laos
LV|Latvia
LB|Lebanon
LS|Lesotho
LR|Liberia
LY|Libya
LI|Liechtenstein
LT|Lithuania
LU|Luxembourg
MO|Macao
MG|Madagascar
MW|Malawi
MY|Malaysia
MV|Maldives
ML|Mali
MT|Malta
MH|Marshall Islands
MR|Mauritania
MU|Mauritius
MX|Mexico
FM|Micronesia
MD|Moldova
MC|Monaco
MN|Mongolia
ME|Montenegro
MA|Morocco
MZ|Mozambique
MM|Myanmar
NA|Namibia
NR|Nauru
NP|Nepal
NL|Netherlands
NZ|New Zealand
NI|Nicaragua
NE|Niger
NG|Nigeria
KP|North Korea
MK|North Macedonia
NO|Norway
OM|Oman
PK|Pakistan
PW|Palau
PS|Palestine
PA|Panama
PG|Papua New Guinea
PY|Paraguay
PE|Peru
PH|Philippines
PL|Poland
PT|Portugal
QA|Qatar
RO|Romania
RU|Russia
RW|Rwanda
KN|Saint Kitts and Nevis
LC|Saint Lucia
VC|Saint Vincent and the Grenadines
WS|Samoa
SM|San Marino
ST|São Tomé and Príncipe
SA|Saudi Arabia
SN|Senegal
RS|Serbia
SC|Seychelles
SL|Sierra Leone
SG|Singapore
SK|Slovakia
SI|Slovenia
SB|Solomon Islands
SO|Somalia
ZA|South Africa
KR|South Korea
SS|South Sudan
ES|Spain
LK|Sri Lanka
SD|Sudan
SR|Suriname
SE|Sweden
CH|Switzerland
SY|Syria
TW|Taiwan
TJ|Tajikistan
TZ|Tanzania
TH|Thailand
TL|Timor-Leste
TG|Togo
TO|Tonga
TT|Trinidad and Tobago
TN|Tunisia
TR|Türkiye
TM|Turkmenistan
TV|Tuvalu
UG|Uganda
UA|Ukraine
AE|United Arab Emirates
GB|United Kingdom
US|United States
UY|Uruguay
UZ|Uzbekistan
VU|Vanuatu
VA|Vatican City
VE|Venezuela
VN|Vietnam
YE|Yemen
ZM|Zambia
ZW|Zimbabwe
"""

COUNTRIES = [tuple(line.split("|", 1)) for line in _COUNTRY_DATA.strip().splitlines()]
COUNTRY_NAMES = dict(COUNTRIES)

# Shown first in the dropdown (before the full A-Z list).
POPULAR_COUNTRIES = ["PK", "IN", "US", "GB", "BD", "SA", "AE", "CA", "AU"]


# ---------------------------------------------------------------------------
# Checksums (mirrored in index.html -> ID_CHECKS)
# ---------------------------------------------------------------------------
def _digits(s):
    return [int(c) for c in s]


def _luhn(s):
    total = 0
    for i, d in enumerate(reversed(_digits(s))):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def _verhoeff(s):  # India - Aadhaar
    c = 0
    for i, d in enumerate(reversed(_digits(s))):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][d]]
    return c == 0


def _tfn(s):  # Australia - 9-digit TFN (older 8-digit TFNs: format only)
    if len(s) != 9:
        return True
    w = [1, 4, 3, 7, 5, 8, 6, 9, 10]
    return sum(a * b for a, b in zip(_digits(s), w)) % 11 == 0


def _cn18(s):  # China - resident ID, ISO 7064 mod 11-2
    w = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    total = sum(int(c) * k for c, k in zip(s[:17], w))
    return "10X98765432"[total % 11] == s[17]


def _tr11(s):  # Türkiye - T.C. Kimlik No
    d = _digits(s)
    d10 = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    return d10 == d[9] and sum(d[:10]) % 10 == d[10]


def _ir10(s):  # Iran - Melli code
    if len(set(s)) == 1:
        return False
    d = _digits(s)
    r = sum(d[i] * (10 - i) for i in range(9)) % 11
    return d[9] == (r if r < 2 else 11 - r)


def _cpf(s):  # Brazil - CPF
    if len(set(s)) == 1:
        return False
    d = _digits(s)
    for n in (9, 10):
        total = sum(d[i] * (n + 1 - i) for i in range(n))
        check = (total * 10) % 11
        if check == 10:
            check = 0
        if check != d[n]:
            return False
    return True


def _dni(s):  # Spain - DNI / NIE
    letters = "TRWAGMYFPDXBNJZSQVHLCKE"
    body = s[:-1]
    if body[0] in "XYZ":
        body = str("XYZ".index(body[0])) + body[1:]
    return letters[int(body) % 23] == s[-1]


def _nl11(s):  # Netherlands - BSN (elfproef)
    w = [9, 8, 7, 6, 5, 4, 3, 2, -1]
    total = sum(a * b for a, b in zip(_digits(s), w))
    return total != 0 and total % 11 == 0


def _th13(s):  # Thailand - citizen ID
    d = _digits(s)
    total = sum(d[i] * (13 - i) for i in range(12))
    return (11 - total % 11) % 10 == d[12]


CHECKS = {
    "luhn": _luhn, "verhoeff": _verhoeff, "tfn": _tfn, "cn18": _cn18, "tr11": _tr11,
    "ir10": _ir10, "cpf": _cpf, "dni": _dni, "nl11": _nl11, "th13": _th13,
}

# Date part shared by several patterns: month 01-12, day 01-31.
_MM = r"(0[1-9]|1[0-2])"
_DD = r"(0[1-9]|[12]\d|3[01])"


# ---------------------------------------------------------------------------
# National-ID rules
#   label    : name of the document shown on the form
#   hint     : short rule shown next to the label, e.g. "13 digits"
#   detail   : optional extra rule, only shown in the error message
#   example  : placeholder
#   pattern  : regex the cleaned value (spaces/dashes removed, upper-case) must match
#   charset  : "digits" or "alnum" - what the input box accepts
#   max      : maximum length of the cleaned value
#   check    : optional checksum name from CHECKS
# ---------------------------------------------------------------------------
ID_RULES = {
    "PK": dict(label="CNIC Number", hint="13 digits", detail="first digit 1-8", example="3520212345671",
               pattern=r"^[1-8]\d{12}$", charset="digits", max=13),
    "IN": dict(label="Aadhaar Number", hint="12 digits", detail="cannot start with 0 or 1", example="234567890124",
               pattern=r"^[2-9]\d{11}$", charset="digits", max=12, check="verhoeff"),
    "US": dict(label="Social Security Number", hint="SSN, 9 digits", example="123456789",
               pattern=r"^(?!000|666|9\d\d)\d{3}(?!00)\d{2}(?!0000)\d{4}$", charset="digits", max=9),
    "GB": dict(label="National Insurance Number", hint="NINO: 2 letters + 6 digits + 1 letter",
               example="AB123456C",
               pattern=r"^(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]\d{6}[A-D]$",
               charset="alnum", max=9),
    "BD": dict(label="National ID Number", hint="NID: 10, 13 or 17 digits", example="1234567890",
               pattern=r"^(\d{10}|\d{13}|\d{17})$", charset="digits", max=17),
    "SA": dict(label="National ID / Iqama Number", hint="10 digits", detail="starts with 1 or 2", example="1234567890",
               pattern=r"^[12]\d{9}$", charset="digits", max=10),
    "AE": dict(label="Emirates ID Number", hint="15 digits", detail="starts with 784", example="784199012345671",
               pattern=r"^784\d{12}$", charset="digits", max=15),
    "CA": dict(label="Social Insurance Number", hint="SIN, 9 digits", example="130692544",
               pattern=r"^[1-79]\d{8}$", charset="digits", max=9, check="luhn"),
    "AU": dict(label="Tax File Number", hint="TFN, 8 or 9 digits", example="123456782",
               pattern=r"^\d{8,9}$", charset="digits", max=9, check="tfn"),
    "CN": dict(label="Resident Identity Card Number", hint="18 characters (17 digits + digit or X)",
               example="11010519491231002X",
               pattern=r"^\d{6}(19|20)\d{2}" + _MM + _DD + r"\d{3}[\dX]$", charset="alnum", max=18, check="cn18"),
    "TR": dict(label="T.C. Kimlik No", hint="11 digits", detail="cannot start with 0", example="10000000146",
               pattern=r"^[1-9]\d{10}$", charset="digits", max=11, check="tr11"),
    "IR": dict(label="Melli Code", hint="10-digit national code", example="0499370899",
               pattern=r"^\d{10}$", charset="digits", max=10, check="ir10"),
    "EG": dict(label="National ID Number", hint="14 digits", detail="starts with 2 or 3", example="29001011234567",
               pattern=r"^[23]\d{13}$", charset="digits", max=14),
    "KR": dict(label="Resident Registration Number", hint="13 digits (YYMMDD + 7 digits)", example="9001011234567",
               pattern=r"^\d{2}" + _MM + _DD + r"[1-8]\d{6}$", charset="digits", max=13),
    "BR": dict(label="CPF Number", hint="11 digits", example="52998224725",
               pattern=r"^\d{11}$", charset="digits", max=11, check="cpf"),
    "ES": dict(label="DNI / NIE Number", hint="8 digits + letter, or X/Y/Z + 7 digits + letter",
               example="12345678Z", pattern=r"^(\d{8}|[XYZ]\d{7})[A-Z]$", charset="alnum", max=9, check="dni"),
    "ID": dict(label="NIK Number", hint="16 digits", example="3171234567890001",
               pattern=r"^\d{16}$", charset="digits", max=16),
    "NG": dict(label="National Identification Number", hint="NIN, 11 digits", example="12345678901",
               pattern=r"^\d{11}$", charset="digits", max=11),
    "ZA": dict(label="South African ID Number", hint="13 digits (YYMMDD + 7 digits)", example="8001015009087",
               pattern=r"^\d{2}" + _MM + _DD + r"\d{7}$", charset="digits", max=13, check="luhn"),
    "SG": dict(label="NRIC / FIN", hint="letter + 7 digits + letter", example="S1234567D",
               pattern=r"^[STFGM]\d{7}[A-Z]$", charset="alnum", max=9),
    "MY": dict(label="MyKad Number", hint="12 digits (YYMMDD + 6 digits)", example="900101011234",
               pattern=r"^\d{2}" + _MM + _DD + r"\d{6}$", charset="digits", max=12),
    "LK": dict(label="NIC Number", hint="9 digits + V/X, or 12 digits",
               example="853202114V", pattern=r"^(\d{9}[VX]|\d{12})$", charset="alnum", max=12),
    "MX": dict(label="CURP", hint="18 characters", example="GOMC900101HDFRRL09",
               pattern=r"^[A-Z]{4}\d{6}[HMX][A-Z]{5}[A-Z0-9]\d$", charset="alnum", max=18),
    "IT": dict(label="Codice Fiscale", hint="16 characters", example="RSSMRA80A01H501U",
               pattern=r"^[A-Z]{6}[A-Z0-9]{2}[A-EHLMPR-T][A-Z0-9]{2}[A-Z][A-Z0-9]{3}[A-Z]$",
               charset="alnum", max=16),
    "JP": dict(label="My Number", hint="12 digits", example="123456789012",
               pattern=r"^\d{12}$", charset="digits", max=12),
    "NL": dict(label="BSN Number", hint="9 digits", example="111222333",
               pattern=r"^\d{9}$", charset="digits", max=9, check="nl11"),
    "TH": dict(label="Thai National ID Number", hint="13 digits", detail="first digit 1-8", example="1101700230708",
               pattern=r"^[1-8]\d{12}$", charset="digits", max=13, check="th13"),
    "VN": dict(label="CCCD Number", hint="12 digits", example="001090012345",
               pattern=r"^\d{12}$", charset="digits", max=12),
    "KE": dict(label="National ID Number", hint="7 or 8 digits", example="12345678",
               pattern=r"^\d{7,8}$", charset="digits", max=8),
}

GENERIC_RULE = dict(label="National ID / Passport Number", hint="6-20 letters or digits",
                    example="A1234567", pattern=r"^[A-Z0-9]{6,20}$", charset="alnum", max=20)


def get_rule(country_code):
    return ID_RULES.get(country_code, GENERIC_RULE)


def clean_id(value):
    """Remove spaces, dashes, dots and slashes and upper-case the value."""
    return re.sub(r"[\s\-./]", "", str(value or "")).upper()


def normalize_country(code):
    """Return the ISO code if it is a known country, otherwise None."""
    code = str(code or "").strip().upper()
    return code if code in COUNTRY_NAMES else None


def error_message(country_code):
    """The text shown when an ID does not match - identical to the one built in index.html."""
    rule = get_rule(country_code)
    rules_text = rule["hint"] + ("; " + rule["detail"] if rule.get("detail") else "")
    return (f"Please enter a valid {rule['label']} for {COUNTRY_NAMES[country_code]} "
            f"({rules_text}, e.g. {rule['example']}).")


def validate_national_id(country_code, value):
    """
    Check an ID number against the rules of the chosen country.
    Returns (ok, cleaned_value, error_message).
    """
    code = normalize_country(country_code)
    if not code:
        return False, "", "Please select your country."
    rule = get_rule(code)
    cleaned = clean_id(value)
    valid = bool(re.fullmatch(rule["pattern"], cleaned))
    if valid and rule.get("check"):
        try:
            valid = bool(CHECKS[rule["check"]](cleaned))
        except (ValueError, IndexError):
            valid = False
    if not valid:
        return False, cleaned, error_message(code)
    return True, cleaned, ""


def public_config():
    """Everything the browser needs to build the country picker and validate IDs."""
    rules = {}
    for code, _name in COUNTRIES:
        rules[code] = get_rule(code)
    # Send each distinct rule once; countries just point at one.
    unique, index = [], {}
    country_rule = {}
    for code, rule in rules.items():
        key = id(rule)
        if key not in index:
            index[key] = len(unique)
            unique.append(rule)
        country_rule[code] = index[key]
    return {
        "countries": [{"code": c, "name": n} for c, n in COUNTRIES],
        "popular": POPULAR_COUNTRIES,
        "rules": unique,
        "countryRule": country_rule,
    }
