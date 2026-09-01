# Frontier Agent: Complete Walkthrough

## What Does Frontier Do?

Frontier is the "decision maker" of the form-filling pipeline. Think of it like a very smart checklist:
- The form has choices (branching points): "Choose your entity type: LLC or Corporation?"
- Frontier says: "OK, I'll try LLC first, see what happens. Then try Corporation."
- For each choice, it asks the FormFiller agent: "Click this option" or "Fill these fields" or "Click Next"
- Frontier watches the page before/after and decides: did anything change? Keep going or record this walk?

**No LLM, no magic.** Just rules and logic.

---

## The High-Level Flow

```
Loop (orchestrator)
  ↓
  Scraper reads page → PageDescription
  ↓
  Frontier sees what's on the page
  ↓
  Frontier decides ONE action (e.g., "set_option: LLC")
  ↓
  FormFiller executes the action on the page
  ↓
  Loop compares page before/after → Diff
  ↓
  Frontier sees the Diff (did page change?)
  ↓
  Frontier decides next action (keep filling? or send walk slice to ReplayGen?)
  ↓
  (repeat until form is complete)
```

---

## Key Concepts

### 1. **Control**
A single form field on a page.
```python
Control(
    fieldId="q_001",           # unique ID
    label="Business Name",      # human-readable
    type="text",               # what kind? (text, select, toggle, etc.)
    required=True,             # must be filled?
    locator="#businessName",   # where to find it on the page
    options=[],                # if select/toggle, the choices
)
```

### 2. **Gate**
A branching point Frontier is exploring. Only select/toggle fields with 2+ options.
```python
Gate(
    gateId="g_entity",
    fieldId="q_entity_type",
    options=["LLC", "Corporation"],
    walked=["LLC"],              # tried this one
    pending=["Corporation"],     # still to try
)
```

**Frontier's strategy**: Try each option one at a time, watch the page change, record it, try the next.

### 3. **FrontierBoard**
Frontier's memory: the state of the entire walk so far.
```python
FrontierBoard(
    gates=[...list of gates...],
    currentStageId="form_page_1",
    status="exploring",  # exploring | awaiting_fill | slice_stable | complete | blocked
)
```

### 4. **Assignment**
One instruction Frontier gives to FormFiller.

```python
# "Click the LLC option"
SetOptionAssignment(gateId="g_entity", option="LLC", locator="#entityType")

# "Fill these required fields"
FillPageAssignment(applicantSlice={"q_001": "Harbor Point Bistro"})

# "Click Next, Back, Submit, or Stop"
SimpleAssignment(type="next")
```

### 5. **Diff**
What changed on the page after FormFiller executed an assignment.
```python
Diff(
    polarity="+ve",  # page changed (more work to do)
    addedControls=[...],     # new fields appeared
    removedControls=[...],   # fields disappeared
    changedControls=[...],   # fields changed
)
```

**Polarity explained:**
- `"+ve"` = page changed (revealing new fields, enabling buttons, etc.)
  → Keep exploring, there's more to do here.
- `"-ve"` = page didn't change (validation blocked, or nothing changed)
  → This walk is complete, send to ReplayGen.

### 6. **WalkSlice**
The ordered sequence of actions for one successful walk through the form.

```python
# Example: walking the "LLC" option
[
    WalkStep(action="choose", option="LLC", locator="#entityType"),
    WalkStep(action="type", canonical="business.legal_name", value="Harbor Point Bistro"),
    WalkStep(action="click", locator="button:has-text('Next')"),
]
```

ReplayGen turns this into a reusable script.

---

## How Frontier Works: Step by Step

### Scenario: A form with Entity Type (LLC/Corp) and Business Name

#### **Page 1 appears** (Loop calls `Frontier.on_page_description()`)
```
Page:
  - Entity Type (select): [LLC, Corporation]  ← gate!
  - Business Name (text): [empty]  ← required field
  - Next button: yes
```

Frontier's logic:
1. Find gates: "Entity Type has 2 options → gate"
2. Create gate with `pending=["LLC", "Corporation"]`
3. Decide next action:
   - Is there a gate with pending options? **Yes!**
   - Pop LLC from pending, emit: `SetOptionAssignment(option="LLC")`

Loop gives FormFiller: **"Click LLC"**

---

#### **After LLC is clicked** (Loop calls `Frontier.on_diff()`)
```
Page changed:
  - Entity Type: LLC (now selected)
  - Business Name: still empty
  - LLC Members (new field appeared!) ← revealed by LLC choice
  - Next button: still there
```

Loop compares before/after: **Diff(polarity="+ve")** ← page changed

Frontier's logic:
1. "LLC is done, move it to walked"
2. Gate now: `walked=["LLC"], pending=["Corporation"]`
3. Set status to "exploring"

Loop calls `Frontier.on_page_description()` again with updated page.

---

#### **Second assignment decision**
```
Page now has:
  - Entity Type: LLC (selected)
  - Business Name: [empty, required]
  - LLC Members: [empty, required]  ← revealed
  - Next: yes
```

Frontier's logic:
1. Gate still has `pending=["Corporation"]`
2. Decide next action:
   - Pop Corporation, emit: `SetOptionAssignment(option="Corporation")`

Loop gives FormFiller: **"Click Corporation"**

---

#### **After Corporation is clicked** (Loop calls `Frontier.on_diff()`)
```
Page changed:
  - Entity Type: Corporation (now selected)
  - Business Name: still empty
  - LLC Members: DISAPPEARED (was only for LLC)
  - Corp Board Members (new field!):  ← revealed by Corp choice
  - Next: still there
```

Loop compares: **Diff(polarity="+ve")** ← page changed again

Frontier moves Corporation to walked: `walked=["LLC", "Corporation"], pending=[]`

Gate is now fully explored! ✓

---

#### **Third assignment decision**
```
Current gate is exhausted.
Page has unfilled required fields: Business Name, Corp Board Members
```

Frontier's logic:
1. No more pending options in any gate
2. Are there unfilled required fields? **Yes!**
3. Emit: `FillPageAssignment(applicantSlice={"q_001": "", "q_corp_members": ""})`

Loop gives FormFiller: **"Fill these fields"**

---

#### **After fields are filled** (Loop calls `Frontier.on_diff()`)
```
Page:
  - Entity Type: Corporation
  - Business Name: Harbor Point Bistro
  - Corp Board Members: John Smith
```

Loop compares: **Diff(polarity="-ve")** ← no new fields, page settled

Frontier's logic:
1. Page is stable
2. Mark `status="slice_stable"`
3. Build walk slice (ordered actions for this walk)
4. **Return the WalkSlice to ReplayGen**

ReplayGen compiles it into a Playwright script. ✓

---

#### **Fourth assignment decision** (back to `on_page_description()`)
```
All fields filled, no blockers, page is stable.
```

Frontier's logic:
1. No gates with pending
2. No unfilled required fields
3. Is there a Next button? **Yes!**
4. Emit: `SimpleAssignment(type="next")`

Loop gives FormFiller: **"Click Next"**

---

#### **Form complete** (if next page doesn't exist or is final)
```
If this is the last page, emit: `SimpleAssignment(type="submit")`
Frontier marks `status="complete"`
```

---

## The Code Structure

```
src/trailblazer/
├── contracts/__init__.py
│   └── All data shapes: Control, PageDescription, Gate, FrontierBoard,
│       Assignment (union), Diff, WalkSlice
│
├── agents/frontier/
│   ├── frontier.py
│   │   └── FrontierAgent class
│   │       ├── on_page_description() ← Loop calls this
│   │       └── on_diff() ← Loop calls this
│   │
│   └── board.py
│       └── FrontierBoardState class (pure logic)
│           ├── identify_gates()
│           ├── next_assignment_for_page()
│           └── apply_diff()
```

### FrontierAgent (frontier.py)
The public interface that Loop talks to. Thin wrapper around FrontierBoardState.

**Methods:**
- `on_page_description(job, page)` → `(board, assignment)`
  - Called when Scraper gives us a page
  - Returns updated board state + next action
- `on_diff(job, diff, assignment)` → `(board, assignment_or_slice)`
  - Called when page before/after are compared
  - Returns updated board state + next action or walk slice

### FrontierBoardState (board.py)
The pure logic engine. No I/O, no side effects.

**Methods:**
- `identify_gates(page)` → `list[Gate]`
  - Find branching points (select/toggle with 2+ options)
- `next_assignment_for_page(page)` → `Assignment`
  - Decide the next single action given current page state
  - Decision tree: blockers? → gate pending? → unfilled fields? → next? → submit
- `apply_diff(diff, assignment)` → `(action_or_slice, is_slice_bool)`
  - React to page change (+ve: keep going; -ve: finalize walk)

---

## v0 Scope (What's Implemented)

✅ **Done:**
- Identify gates from candidateGates
- Walk one gate's options sequentially
- Fill required non-gate fields
- React to page changes (+ve/-ve)
- Return walk slices when a walk stabilizes

❌ **Not yet (v1):**
- Backtracking: when a gate option is fully walked, undoing it to try the next
- Multi-gate juggling: walking multiple gates in the right order (one at a time, no cartesian product)
- Revealed field cleanup: dropping fields that only existed for the old gate option
- Full walk history: accumulating all actions into walk_log for ReplayGen

---

## Testing

16 unit tests covering:
- Gate identification from pages
- Assignment selection logic
- Diff reaction (walk progression)
- Board serialization/persistence
- Multi-gate invariants

Run: `uv run pytest tests/agents/frontier/ -v`

---

## Summary

**Frontier is a state machine** that:
1. Tracks form gates (branching choices)
2. Walks each gate's options one at a time
3. Watches the page to see if things changed
4. Decides what to do next based on form structure and state
5. Emits one assignment at a time to FormFiller
6. Returns walk slices to ReplayGen when a walk is complete

**No AI, no randomness, no guessing.** Pure rules and logic, fully testable, fully observable.
