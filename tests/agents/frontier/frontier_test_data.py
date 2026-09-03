"""
Sample PageDescription payloads for Frontier tests.

Raw dicts (not Pydantic models) so tests can freely mutate/reuse them
and construct PageDescription(**PAGE_1_BUSINESS_INFO) as needed.

- PAGE_SIMPLE / PAGE_SIMPLE_2: the minimal Name/Gender/Email walkthrough, small
  enough to assert an exact assignment sequence against.
- PAGE_1_BUSINESS_INFO: a real live scrape of the Pie Insurance business-info page.

Both cover the two ways a control's options can reach Frontier:
- options is None   -> unknown. FormFiller may discover it's really a chooser and
                       report the options back (q_gender; q_001 and q_006).
- options is a list -> Scraper already read them, and each option carries its OWN
                       locator, which is what must be used to select it — not the
                       parent control's (q_consent; q_009).
"""

# ---------------------------------------------------------------------------
# The three-field walkthrough, exactly as specified: Name, Gender, Email.
#
# Nothing here is a "gate" as far as Scraper can tell — all three report
# options: null. Gender only turns out to be a dropdown when FormFiller tries to
# fill it. next is None, so a fully-explored page ends the walk.
# ---------------------------------------------------------------------------

PAGE_NAME_GENDER_EMAIL = {
    "stageId": "basic_page",
    "url": "https://example.test/basic",
    "controls": [
        {
            "fieldId": "q_name",
            "label": "Name",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#name",
            "unique": True,
            "revealedBy": None,
        },
        {
            # Scraper sees a control it can't read options from. FormFiller will
            # discover it's a dropdown with Male / Female.
            "fieldId": "q_gender",
            "label": "Gender",
            "type": "select",
            "required": True,
            "options": None,
            "locator": "#gender",
            "unique": True,
            "revealedBy": None,
        },
        {
            "fieldId": "q_email",
            "label": "Email",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#email",
            "unique": True,
            "revealedBy": None,
        },
    ],
    "next": None,
    "back": None,
    "candidateGates": [],
    "blockers": [],
}

# What FormFiller finds when it opens Gender.
GENDER_OPTIONS = [
    {"label": "Male", "locator": "#gender-male"},
    {"label": "Female", "locator": "#gender-female"},
]


# The Name / Gender / Email walkthrough.
PAGE_SIMPLE = {
    "stageId": "simple_page_1",
    "url": "https://example.test/simple/1",
    "controls": [
        {
            "fieldId": "q_name",
            "label": "Name",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#name",
            "unique": True,
            "revealedBy": None,
        },
        {
            # Looks like a plain field to Scraper. FormFiller will find out it's
            # a dropdown with Male/Female and report those back.
            "fieldId": "q_gender",
            "label": "Gender",
            "type": "select",
            "required": True,
            "options": None,
            "locator": "#gender",
            "unique": True,
            "revealedBy": None,
        },
        {
            # Options ARE in the PageDescription, each with its own locator.
            # Selecting "Yes" must click #consent-yes, NOT #consent.
            "fieldId": "q_consent",
            "label": "Consent to contact",
            "type": "select",
            "required": True,
            "options": [
                {"label": "Yes", "locator": "#consent-yes"},
                {"label": "Maybe", "locator": "#consent-maybe"},
            ],
            "locator": "#consent",
            "unique": True,
            "revealedBy": None,
        },
        {
            "fieldId": "q_email",
            "label": "Email",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#email",
            "unique": True,
            "revealedBy": None,
        },
    ],
    "next": 'button:has-text("Next")',
    "back": None,
    "candidateGates": [],
    "blockers": [],
}

# Second page: no Next button, so a fully-explored version of this ends the walk.
PAGE_SIMPLE_2 = {
    "stageId": "simple_page_2",
    "url": "https://example.test/simple/2",
    "controls": [
        {
            "fieldId": "q_phone",
            "label": "Phone",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#phone",
            "unique": True,
            "revealedBy": None,
        },
        {
            "fieldId": "q_start",
            "label": "Start Date",
            "type": "date",
            "required": False,
            "options": None,
            "locator": "#startDate",
            "unique": True,
            "revealedBy": None,
        },
    ],
    "next": None,
    "back": 'button:has-text("Back")',
    "candidateGates": [],
    "blockers": [],
}

# A control that only appears once q_gender is set — used to test that revealed
# fields join the exploration queue and get explored before the page finishes.
REVEALED_PRONOUNS = {
    "fieldId": "q_pronouns",
    "label": "Preferred Pronouns",
    "type": "text",
    "required": False,
    "options": None,
    "locator": "#pronouns",
    "unique": True,
    "revealedBy": {"fieldId": "q_gender", "equals": "Female"},
}

# ---------------------------------------------------------------------------
# The real thing: a live scrape of the Pie Insurance business-info page.
#
# Captured 2026-09-02 from a logged-in session over CDP. Kept byte-faithful to
# what Scraper actually emits, including the `_meta` provenance block — which is
# NOT part of the PageDescription contract. Pydantic ignores unknown keys, so
# PageDescription(**PAGE_1_BUSINESS_INFO) parses and drops it, which is what
# MASTER.md wants ("tree and snapshot refs are thrown away").
#
# Three things make this payload the interesting test case:
#   q_001, q_006  custom dropdowns that don't render their options until opened,
#                 so Scraper reports options: null. FormFiller discovers them.
#   q_009         options ARE in the PD, each with its own locator. Note the
#                 control's own locator is the same string as the "Yes" option's
#                 — it's a split control (paired Yes/No elements), so selecting
#                 "No" MUST use the option's locator, not the control's.
#   required      real flags this time, not all False.
# ---------------------------------------------------------------------------

PAGE_1_BUSINESS_INFO = {
    "_meta": {
        "perceivedAt": "2026-09-02T20:55:13+05:00",
        "jobId": "002f603f14b2",
        "source": "live Pie portal, logged-in session, attached over CDP port 9456",
        "producedBy": "trailblazer.agents.scraper.scraper.perceive (DomSnapshotPerceiver)",
    },
    "stageId": "form_page_1_business_info",
    "url": "https://partner.pieinsurance.com/work-comp/business-info",
    "controls": [
        {
            "fieldId": "q_001",
            "label": "Agency / Program",
            "type": "other",
            "required": True,
            "options": None,
            "locator": "#agencyProgram",
            "unique": True,
            "revealedBy": None,
        },
        {
            "fieldId": "q_002",
            "label": "Policy Effective Date",
            "type": "date",
            "required": True,
            "options": None,
            "locator": "#effectiveDate",
            "unique": True,
            "revealedBy": None,
        },
        {
            "fieldId": "q_003",
            "label": "Business Zip Code",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#businessZipCode",
            "unique": True,
            "revealedBy": None,
        },
        {
            "fieldId": "q_004",
            "label": "Legal Business Name",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#businessName",
            "unique": True,
            "revealedBy": None,
        },
        {
            "fieldId": "q_005",
            "label": "DBA (Doing Business As)",
            "type": "text",
            "required": False,
            "options": None,
            "locator": "#dba-0",
            "unique": True,
            "revealedBy": None,
        },
        {
            "fieldId": "q_006",
            "label": "Legal Entity Type",
            "type": "other",
            "required": True,
            "options": None,
            "locator": "#entityType",
            "unique": True,
            "revealedBy": None,
        },
        {
            "fieldId": "q_007",
            "label": "FEIN",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#fein",
            "unique": True,
            "revealedBy": None,
        },
        {
            "fieldId": "q_008",
            "label": "Target or Incumbent Premium",
            "type": "number",
            "required": False,
            "options": None,
            "locator": "#targetPremium",
            "unique": True,
            "revealedBy": None,
        },
        {
            "fieldId": "q_009",
            "label": "Does this business have multiple locations?",
            "type": "select",
            "required": True,
            "options": [
                {"label": "Yes", "locator": 'internal:label="Yes"i'},
                {"label": "No", "locator": 'internal:label="No"i'},
            ],
            "locator": 'internal:label="Yes"i',
            "unique": True,
            "revealedBy": None,
        },
    ],
    "next": 'button:has-text("Next")',
    "back": None,
    "candidateGates": ["q_009"],
    "blockers": [],
}

# Options the real dropdowns reveal once FormFiller opens them. Not part of the
# PageDescription — this is what the filler reports back on discoveredOptions.
PIE_DISCOVERABLE = {
    "q_001": [
        {"label": "Pie Direct", "locator": 'role=option[name="Pie Direct"]'},
        {"label": "Pie Partner Program", "locator": 'role=option[name="Pie Partner Program"]'},
    ],
    "q_006": [
        {
            "label": "Limited Liability Company",
            "locator": '#entityType >> option:has-text("Limited Liability Company")',
        },
        {
            "label": "Corporation",
            "locator": '#entityType >> option:has-text("Corporation")',
        },
        {
            "label": "Sole Proprietor",
            "locator": '#entityType >> option:has-text("Sole Proprietor")',
        },
    ],
}

# Answering "Does this business have multiple locations?" = Yes reveals a count
# field. Exercises the revealed-control path on real data.
PIE_REVEALED_LOCATION_COUNT = {
    "fieldId": "q_012",
    "label": "How many locations?",
    "type": "number",
    "required": True,
    "options": None,
    "locator": "#locationCount",
    "unique": True,
    "revealedBy": {"fieldId": "q_009", "equals": "Yes"},
}


# ---------------------------------------------------------------------------
# Login pages. Login is page 1 of the same chain (MASTER.md), so the walk starts
# on a `login_*` stage. The Scraper marks credential controls with `credential`;
# Frontier fills those from the store by key and leaves everything else on the
# page alone. FieldIds are unique across pages here, as in the fixtures above.
# ---------------------------------------------------------------------------

# One-page sign-in: username, password, a "remember me" toggle Frontier must not
# touch, and a Sign in button as `next`.
LOGIN_PAGE = {
    "stageId": "login_sign_in",
    "url": "https://portal.example.test/login",
    "controls": [
        {
            "fieldId": "q_user",
            "label": "Email address",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#username",
            "unique": True,
            "revealedBy": None,
            "credential": "username",
        },
        {
            "fieldId": "q_pass",
            "label": "Password",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#password",
            "unique": True,
            "revealedBy": None,
            "credential": "password",
        },
        {
            "fieldId": "q_remember",
            "label": "Remember me",
            "type": "toggle",
            "required": False,
            "options": None,
            "locator": "#remember",
            "unique": True,
            "revealedBy": None,
        },
    ],
    "next": 'button:has-text("Sign in")',
    "back": None,
    "candidateGates": [],
    "blockers": [],
}

# The delivery-choice screen some identity providers show before the code:
# "how should we send it?" Frontier must pick Email once and never try the
# text-message option, because the inbox it reads only ever sees email.
LOGIN_CHANNEL_PAGE = {
    "stageId": "login_verify_method",
    "url": "https://portal.example.test/mfa/choose",
    "controls": [
        {
            "fieldId": "q_channel",
            "label": "How would you like to receive your code?",
            "type": "select",
            "required": True,
            "options": [
                {"label": "Text message (SMS)", "locator": "#channel-sms"},
                {"label": "Email", "locator": "#channel-email"},
            ],
            "locator": "#channel",
            "unique": True,
            "revealedBy": None,
        },
    ],
    "next": 'button:has-text("Send code")',
    "back": None,
    "candidateGates": ["q_channel"],
    "blockers": [],
}

# The one-time-code page. `credential: otp` is filled with LOGIN_OTP; the
# "Resend code" chooser is not something to explore.
LOGIN_OTP_PAGE = {
    "stageId": "login_verify_code",
    "url": "https://portal.example.test/mfa/code",
    "controls": [
        {
            "fieldId": "q_code",
            "label": "Verification code",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#code",
            "unique": True,
            "revealedBy": None,
            "credential": "otp",
        },
        {
            "fieldId": "q_resend",
            "label": "Didn't get it?",
            "type": "select",
            "required": False,
            "options": [{"label": "Resend code", "locator": "#resend"}],
            "locator": "#resend-group",
            "unique": True,
            "revealedBy": None,
        },
    ],
    "next": 'button:has-text("Verify")',
    "back": None,
    "candidateGates": ["q_resend"],
    "blockers": [],
}

# Two-step sign-in on ONE stage name: the username page, then a password field
# appears on the same URL. Not a rejection -- the credential controls changed.
LOGIN_STEP_USERNAME = {
    "stageId": "login_sign_in",
    "url": "https://idp.example.test/u/login",
    "controls": [
        {
            "fieldId": "q_user_a",
            "label": "Username",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#signInName",
            "unique": True,
            "revealedBy": None,
            "credential": "username",
        },
    ],
    "next": 'button:has-text("Continue")',
    "back": None,
    "candidateGates": [],
    "blockers": [],
}

LOGIN_STEP_PASSWORD = {
    "stageId": "login_sign_in",
    "url": "https://idp.example.test/u/login",
    "controls": [
        {
            "fieldId": "q_pass_a",
            "label": "Password",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#password",
            "unique": True,
            "revealedBy": None,
            "credential": "password",
        },
    ],
    "next": 'button:has-text("Continue")',
    "back": None,
    "candidateGates": [],
    "blockers": [],
}

# A second host that asks the agency to sign in again after the first portal
# hands off to it (Chubb's chubbaccess -> Marketplace, CoverForce).
LOGIN_SECOND_HOST = {
    "stageId": "login_marketplace",
    "url": "https://marketplace.example.test/signin",
    "controls": [
        {
            "fieldId": "q_user_b",
            "label": "Agent ID",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#agentId",
            "unique": True,
            "revealedBy": None,
            "credential": "username",
        },
        {
            "fieldId": "q_pass_b",
            "label": "Password",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#pwd",
            "unique": True,
            "revealedBy": None,
            "credential": "password",
        },
    ],
    "next": 'button:has-text("Log in")',
    "back": None,
    "candidateGates": [],
    "blockers": [],
}

# The first form page after login: one plain field and a two-option chooser, so
# a test can see that the login prefix is split out and does not multiply paths.
FORM_AFTER_LOGIN = {
    "stageId": "form_page_1_business_info",
    "url": "https://portal.example.test/app/business-info",
    "controls": [
        {
            "fieldId": "q_name",
            "label": "Legal Business Name",
            "type": "text",
            "required": True,
            "options": None,
            "locator": "#businessName",
            "unique": True,
            "revealedBy": None,
        },
        {
            "fieldId": "q_entity",
            "label": "Entity Type",
            "type": "select",
            "required": True,
            "options": [
                {"label": "LLC", "locator": "#entity-llc"},
                {"label": "Corporation", "locator": "#entity-corp"},
            ],
            "locator": "#entityType",
            "unique": True,
            "revealedBy": None,
        },
    ],
    "next": None,
    "back": None,
    "candidateGates": ["q_entity"],
    "blockers": [],
}
