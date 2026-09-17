"""Merchant categories. A purchase is approved only in a QUALIFIED category."""

QUALIFIED = ("pharmacy", "hospital", "doctor", "dental", "vision", "medical_equipment", "lab")
NOT_QUALIFIED = ("restaurant", "grocery", "electronics", "gas", "entertainment", "retail", "other")
ALL_CATEGORIES = QUALIFIED + NOT_QUALIFIED
