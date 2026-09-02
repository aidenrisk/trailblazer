"""In-memory stand-ins for the two stores Loop's login path writes to.

`FakeProgramStore` mirrors `LoginProgramStore` (insert-only versions, newest
non-degraded is active) without Postgres. `FakeLock` records what was done to it.
"""

from trailblazer.shared.login_programs import LoginProgram


class FakeProgramStore:
    def __init__(self, slug: str | None = None, *programs):
        self.rows: list[LoginProgram] = []
        self.degraded: list[tuple[int, str]] = []
        self.locked: list[int] = []
        for steps in programs:
            self.save(slug, steps)

    def active(self, slug):
        live = [r for r in self.rows if r.carrier_slug == slug and r.status != "degraded"]
        return max(live, key=lambda r: r.version) if live else None

    def save(self, slug, steps, **_):
        version = 1 + max((r.version for r in self.rows if r.carrier_slug == slug), default=0)
        row = LoginProgram(
            id=len(self.rows) + 1, carrier_slug=slug, version=version, status="candidate", steps=list(steps)
        )
        self.rows.append(row)
        return row

    def mark_degraded(self, program_id, reason):
        for r in self.rows:
            if r.id == program_id:
                r.status = "degraded"
                r.degraded_reason = reason
        self.degraded.append((program_id, reason))

    def mark_locked(self, program_id):
        for r in self.rows:
            if r.id == program_id:
                r.status = "locked"
        self.locked.append(program_id)


class FakeLock:
    made: list["FakeLock"] = []

    def __init__(self, slug, url, **kw):
        self.slug = slug
        self.events: list[str] = []
        FakeLock.made.append(self)

    def acquire(self):
        self.events.append("acquire" if self.slug else "noop")
        return self

    def release(self):
        self.events.append("release")
