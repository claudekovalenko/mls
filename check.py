import sys
sys.path.insert(0, "/home/user/mls/scripts")
from search_worker import is_single_family, passes_criteria
from cleanup_houses import reason_to_drop

fails = []


def ok(label, got, want):
    if got != want:
        fails.append(f"{label}: got {got!r}, wanted {want!r}")


H = {"address": "395 Nottingham Dr, Marietta, GA 30066", "beds": 4,
     "baths": 3.5, "sqft": 3584, "price": 475000}


def L(**kw):
    return {**H, **kw}


# --- the whitelist itself -------------------------------------------------
ok("rentcast single family", is_single_family(L(propertyType="Single Family")), True)
ok("reso residence", is_single_family(L(propertyType="Single Family Residence")), True)
ok("reso detached", is_single_family(L(propertyType="Single Family Detached")), True)
ok("lowercase/spacing", is_single_family(L(propertyType="  singlefamily ")), True)

ok("condo", is_single_family(L(propertyType="Condo")), False)
ok("townhouse", is_single_family(L(propertyType="Townhouse")), False)
ok("apartment", is_single_family(L(propertyType="Apartment")), False)
ok("multi-family", is_single_family(L(propertyType="Multi-Family")), False)
ok("land", is_single_family(L(propertyType="Land")), False)

# The whole reason for a whitelist: these used to pass "not a condo".
ok("manufactured", is_single_family(L(propertyType="Manufactured")), False)
ok("mobile", is_single_family(L(propertyType="Mobile Home")), False)
ok("unknown type", is_single_family(L(propertyType="Houseboat")), False)
ok("empty type", is_single_family(L(propertyType="")), False)
ok("missing type", is_single_family({"address": H["address"]}), False)

# A feed mislabelling a stacked unit is still caught by the address.
ok("SF label, unit address",
   is_single_family(L(propertyType="Single Family",
                      address="1812 Ashborough Rd SE, Apt A, Marietta, GA")), False)

# ...but a street that merely contains a marker substring is not.
ok("Unity Ln not a unit",
   is_single_family(L(propertyType="Single Family",
                      address="440 Unity Ln, Marietta, GA 30060")), True)

# --- the gate -------------------------------------------------------------
ok("gate keeps SF", passes_criteria(L(propertyType="Single Family"), {}), True)
ok("gate drops manufactured", passes_criteria(L(propertyType="Manufactured"), {}), False)
ok("gate drops untyped", passes_criteria(L(propertyType=""), {}), False)

# Property Types narrows the search; it can never widen it back to attached
# housing. Naming "Multi-Family" no longer readmits a duplex.
ok("Property Types cannot readmit attached",
   passes_criteria(L(propertyType="Multi-Family"), {"Property Types": "Multi-Family"}), False)
ok("Property Types still narrows within single family",
   passes_criteria(L(propertyType="Single Family"), {"Property Types": "Multi-Family"}), False)
ok("matching single-family type passes",
   passes_criteria(L(propertyType="Single Family"), {"Property Types": "Single Family"}), True)

# Land is still rejected before any of this.
ok("land still rejected",
   passes_criteria({"address": "0 Dirt Rd", "propertyType": "Single Family"}, {}), False)

# --- cleanup --------------------------------------------------------------
SIG = "under $175/sqft, 42% under area $/sqft"

ok("cleanup keeps typed SF",
   reason_to_drop({"Address": H["address"], "Property Type": "Single Family",
                   "Value Signals": SIG}), None)
ok("cleanup drops typed manufactured",
   reason_to_drop({"Address": H["address"], "Property Type": "Manufactured",
                   "Value Signals": SIG}), "not single family (Manufactured)")

# Legacy rows have no Property Type. They must NOT all be deleted.
ok("legacy row survives",
   reason_to_drop({"Address": H["address"], "Value Signals": SIG}), None)
ok("legacy condo address still dropped",
   reason_to_drop({"Address": "1812 Ashborough Rd SE, Apt A, Marietta, GA",
                   "Value Signals": SIG}), "attached housing (condo/townhouse/unit)")

# Protections still hold.
ok("under contract protected",
   reason_to_drop({"Address": H["address"], "Property Type": "Condo",
                   "Value Signals": SIG, "Status": "Under Contract"}), None)
ok("hand-added protected",
   reason_to_drop({"Address": H["address"], "Property Type": "Condo"}), None)

print("\n".join(fails) if fails else f"all {26} checks passed")
sys.exit(1 if fails else 0)
