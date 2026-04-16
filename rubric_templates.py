"""
rubric_templates.py — Hardcoded industry rubric templates.

Ported verbatim from V1. Each template is a starting point an admin can
instantiate into a company-scoped rubric_group. The criteria shapes match
grader.py's V1-style dicts (type: numeric/yes_no/yes_no_pending, scale,
weight, required). The routes layer converts them to V2 ri_score_type
values when creating rubric_items.
"""

RUBRIC_TEMPLATES = {
    "general": {
        "name": "General Customer Service",
        "criteria": [
            {"name": "Speed of Answer",      "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Greeting & Opening",   "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Active Listening",     "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Product Knowledge",    "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Problem Resolution",   "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Empathy & Tone",       "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Closing & Next Steps", "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Overall Impression",   "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Follow-Up Promised",   "type": "yes_no",               "weight": 1.0, "required": True},
            {"name": "Issue Resolved",       "type": "yes_no",               "weight": 1.0, "required": True},
        ],
    },
    "property_management": {
        "name": "Property Management",
        "criteria": [
            {"name": "Speed of Answer",      "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Greeting & Opening",   "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Product Knowledge",    "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Urgency & Motivation", "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Sales Ability",        "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Empathy & Tone",       "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Overall Impression",   "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Tour Offered",         "type": "yes_no",               "weight": 1.0, "required": True},
            {"name": "Follow-Up Promised",   "type": "yes_no",               "weight": 1.0, "required": True},
            {"name": "Follow-Up Delivered",  "type": "yes_no_pending",       "weight": 1.0, "required": True},
        ],
    },
    "retail": {
        "name": "Retail Customer Service",
        "criteria": [
            {"name": "Speed of Answer",        "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Greeting & Opening",     "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Product Knowledge",      "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Problem Resolution",     "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Empathy & Tone",         "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Closing & Next Steps",   "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Overall Impression",     "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Upsell Attempted",       "type": "yes_no",               "weight": 1.0, "required": True},
            {"name": "Return Policy Explained","type": "yes_no",               "weight": 1.0, "required": False},
            {"name": "Issue Resolved",         "type": "yes_no",               "weight": 1.0, "required": True},
        ],
    },
    "tech_support": {
        "name": "Tech Support",
        "criteria": [
            {"name": "Speed of Answer",           "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Greeting & Opening",        "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Active Listening",          "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Technical Knowledge",       "type": "numeric", "scale": 10, "weight": 1.5, "required": True},
            {"name": "Problem Resolution",        "type": "numeric", "scale": 10, "weight": 1.5, "required": True},
            {"name": "Empathy & Tone",            "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Closing & Next Steps",      "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Overall Impression",        "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Issue Diagnosed Correctly", "type": "yes_no",               "weight": 1.0, "required": True},
            {"name": "Escalation Appropriate",    "type": "yes_no",               "weight": 1.0, "required": False},
            {"name": "Issue Resolved",            "type": "yes_no",               "weight": 1.0, "required": True},
        ],
    },
    "healthcare": {
        "name": "Healthcare / Medical Office",
        "criteria": [
            {"name": "Speed of Answer",       "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Greeting & Opening",    "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Active Listening",      "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Knowledge & Accuracy",  "type": "numeric", "scale": 10, "weight": 1.5, "required": True},
            {"name": "Empathy & Tone",        "type": "numeric", "scale": 10, "weight": 1.5, "required": True},
            {"name": "Closing & Next Steps",  "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Overall Impression",    "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Appointment Confirmed", "type": "yes_no",               "weight": 1.0, "required": False},
            {"name": "Insurance Verified",    "type": "yes_no",               "weight": 1.0, "required": False},
            {"name": "Issue Resolved",        "type": "yes_no",               "weight": 1.0, "required": True},
        ],
    },
    "hospitality": {
        "name": "Hospitality / Hotels",
        "criteria": [
            {"name": "Speed of Answer",          "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Greeting & Opening",       "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Product Knowledge",        "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Empathy & Tone",           "type": "numeric", "scale": 10, "weight": 1.5, "required": True},
            {"name": "Upsell / Add-Ons Offered", "type": "numeric", "scale": 10, "weight": 1.0, "required": False},
            {"name": "Closing & Next Steps",     "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Overall Impression",       "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Reservation Confirmed",    "type": "yes_no",               "weight": 1.0, "required": False},
            {"name": "Special Requests Noted",   "type": "yes_no",               "weight": 1.0, "required": False},
            {"name": "Issue Resolved",           "type": "yes_no",               "weight": 1.0, "required": True},
        ],
    },
    "financial": {
        "name": "Financial Services",
        "criteria": [
            {"name": "Speed of Answer",             "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Greeting & Opening",          "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Active Listening",            "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Product Knowledge",           "type": "numeric", "scale": 10, "weight": 1.5, "required": True},
            {"name": "Problem Resolution",          "type": "numeric", "scale": 10, "weight": 1.5, "required": True},
            {"name": "Empathy & Tone",              "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Closing & Next Steps",        "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Overall Impression",          "type": "numeric", "scale": 10, "weight": 1.0, "required": True},
            {"name": "Identity Verified",           "type": "yes_no",               "weight": 1.0, "required": True},
            {"name": "Compliance Disclaimer Given", "type": "yes_no",               "weight": 1.0, "required": True},
            {"name": "Issue Resolved",              "type": "yes_no",               "weight": 1.0, "required": True},
        ],
    },
}


# Map V1 criterion type → V2 ri_score_type.
V1_TO_V2_SCORE_TYPE = {
    "numeric":        "out_of_10",
    "yes_no":         "yes_no",
    "yes_no_pending": "yes_no_pending",
}
