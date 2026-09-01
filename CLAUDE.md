# Trailblazer: Capture Contracts

**5 Agents**: Scraper, Frontier, FormFiller, ReplayGen, Validator. Loop orchestrates.

## System Flow

```
Loop → Scraper → Frontier → FormFiller → Loop → (diff → Frontier → ReplayGen → Validator → Loop)
```

1. **Scraper** reads page → PageDescription (controls, locators, gates, blockers)
2. **Frontier** tracks board state (gates, walked/pending options) → Assignment (one action)
3. **FormFiller** executes Assignment on page → FillReport
4. **Loop** diffs before/after → Frontier (if +ve page changed, loop; if −ve, stop filling)
5. **ReplayGen** compiles walk slice → Program (Playwright IR)
6. **Validator** runs Program on lab fixture → pass/defect/decline/stuck

Illegal edges: scraper→filler, scraper→validator, filler→replayGen, frontier→scraper.

---

## Payloads (Shapes)

**PageDescription** (Scraper)
```json
{
  "stageId": "form_page_1_business_info",
  "url": "https://...",
  "controls": [{
    "fieldId": "q_001",
    "label": "Agency / Program",
    "type": "text|select|toggle|date|number|other",
    "required": true,
    "options": ["opt1", "opt2"],
    "locator": "#agencyProgram",
    "unique": true,
    "revealedBy": { "fieldId": "q_005", "equals": "yes" }
  }],
  "next": "button:has-text(\"Next\")",
  "back": null,
  "candidateGates": [],
  "blockers": []
}
```

**Frontier Board** (Frontier memory)
```json
{
  "gates": [{
    "gateId": "g_entity",
    "fieldId": "q_006",
    "stageId": "form_page_1_business_info",
    "kind": "same-page|last-page",
    "options": ["LLC", "Corp"],
    "walked": ["LLC"],
    "pending": ["Corp"]
  }],
  "currentStageId": "form_page_1_business_info",
  "status": "exploring|awaiting_fill|slice_stable|advancing|backtracking|complete|blocked"
}
```

**Assignment** (Frontier → FormFiller)
```json
{ "type": "set_option", "gateId": "g_entity", "option": "Corp", "locator": "#entityType" }
{ "type": "fill_page", "applicantSlice": { "business.legal_name": "Name" } }
{ "type": "fill_revealed", "fieldId": "q_008", "locator": "#llcMembers", "value": "..." }
{ "type": "next|back|submit|stop" }
{ "type": "last_page_optional_probe" }
```

**FillReport** (FormFiller → Loop)
```json
{
  "ok": true,
  "steps": [{
    "fieldId": "q_004",
    "action": "fill|select|toggle|click|type",
    "locator": "#businessName",
    "value": "...",
    "required": true
  }],
  "advance": false,
  "landed": ["q_004"],
  "errorClass": "not_found|not_unique|widget|validation"
}
```

**Diff** (Loop → Frontier)
```json
{
  "polarity": "+ve|-ve",
  "addedControls": [{ "label": "Members", "locator": "#llcMembers" }],
  "removedControls": [],
  "changedControls": []
}
```
+ve = loop again; −ve = walk to ReplayGen

**Walk Slice** (Frontier → ReplayGen)
Ordered actions: type | choose | toggle | click | wait-for | back. Each includes fieldId, locator, canonical or credentialKey, option if a gate.

**Program** (ReplayGen → Validator)
```json
{
  "language": "playwright-js",
  "ir": [
    { "action": "type", "locator": "#email", "valueFrom": "credential", "credentialKey": "LOGIN_EMAIL" },
    { "action": "click", "locator": "button[type=\"submit\"]" },
    { "action": "type", "locator": "#businessName", "valueFrom": "canonical", "canonical": "business.legal_name" }
  ]
}
```

**Validator Report** (Validator → Loop)
outcome: `pass|defect|decline|stuck`
- pass → Frontier's next (other gate option, Next, or stop)
- defect → patch program (not applicant), retry
- decline → stop, no patch
- stuck → patch at capture; no patch at customer

---

## Contracts Summary

| Contract | From → To | Request | Response | Fail | Then |
|----------|-----------|---------|----------|------|------|
| Look | Loop → Scraper | job, objective (perceive\|post_fill) | status + PageDescription | snapshot failed, page not ready, not unique | → Frontier |
| Update Board | Scraper → Frontier | job + PageDescription | board, pending/walked, Assignment or status | missing locators, not unique | Assignment → FormFiller |
| Execute | Frontier → FormFiller | job, stageId, Assignment | FillReport | not_found, not_unique, widget, validation | Loop scrapes post_fill |
| Diff | Loop → Frontier | prev/new PageDescription, last Assignment | Diff | missing desc, cannot align | +ve: loop; −ve: → ReplayGen |
| Compile | Frontier → ReplayGen | job + walk slice | Program | missing locators, cannot compile | → Validator |
| Run | ReplayGen → Validator | job, Program, lab applicant, credentials | pass\|defect\|decline\|stuck | — | Validator report → Loop |

---

## Key Rules

- **Single assignment**: Filler does not pick the next branch; Frontier decides.
- **Gate groups**: Do not walk combinations of independent gates.
- **Backtrack cleanup**: Drop fields that belonged only to the old option.
- **Applicant immutable**: Validator does not mutate applicant; only patches program.
- **Publish on stop**: Walk + program published when Frontier says stop.
