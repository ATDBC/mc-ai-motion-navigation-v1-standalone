"""Check emitted owner records independently; absence is a coverage gap."""
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from mc2p.motion_nav.async_work import AsyncAdmissionDisposition, AsyncWorkKind
from mc2p.motion_nav.world_model import CellKnowledge


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    INCOMPLETE = "incomplete"
    UNASSESSED = "unassessed"


@dataclass(frozen=True)
class VerificationGap:
    rule: str
    detail: str


@dataclass(frozen=True)
class ObservedAsyncActivity:
    identity: object
    operation: str


@dataclass(frozen=True)
class AsyncCoverageRequirement:
    required_kinds: tuple[AsyncWorkKind, ...] = (AsyncWorkKind.PLANNING,)
    applied_kinds: tuple[AsyncWorkKind, ...] = ()
    no_async_work: bool = False

    def __post_init__(self):
        if (type(self.no_async_work) is not bool
                or type(self.required_kinds) is not tuple or type(self.applied_kinds) is not tuple
                or any(type(kind) is not AsyncWorkKind for kind in (*self.required_kinds, *self.applied_kinds))):
            raise ValueError("asynchronous coverage must be typed")
        if self.no_async_work:
            if self.required_kinds or self.applied_kinds:
                raise ValueError("no-work declaration cannot require asynchronous work")
        elif not self.required_kinds:
            raise ValueError("empty coverage requires an explicit no-work declaration")


@dataclass(frozen=True)
class VerificationAssessment:
    status: VerificationStatus
    coverage: dict
    gaps: tuple[VerificationGap, ...]

    @property
    def complete(self):
        return self.status is VerificationStatus.VERIFIED and not self.gaps


class AsyncInvariantMonitor:
    def __init__(self):
        self.violations = []
        self.coverage = Counter()
        self._windows = {}
        self._finished = {}
        self._applications = set()
        self._seen = set()
        self.last_events = []
        self.by_kind = Counter()

    def check(self, owners, mailboxes=()):
        self.last_events = []
        active = Counter(owner.active_identity for owner in owners if owner.active_identity is not None)
        for identity, count in active.items():
            if count != 1:
                self.violations.append(("I22", "identity has multiple active receivers"))
        for owner in owners:
            if (owner.active_identity is not None
                    and owner.owner_instance_id != owner.active_identity.owner_instance_id):
                self.violations.append(("I22", "active identity belongs to a foreign owner"))
        for identity in mailboxes:
            if active[identity] != 1:
                self.violations.append(("I22", "mailbox requires exactly one active receiving owner"))
        for owner in owners:
            for event in owner.events:
                key = (event.identity, event.operation, event.monotonic_ns, event.cause)
                if key in self._seen:
                    continue
                self._seen.add(key)
                self.last_events.append(event)
                identity = event.identity
                if event.operation == "begin":
                    previous = self._windows.get(identity)
                    if previous is not None:
                        self.violations.append(("I18", "identity restarted or window changed"))
                    self._windows[identity] = event.window
                    self.coverage["begun"] += 1
                    self.by_kind[(identity.work_kind.value, "begin")] += 1
                elif event.operation == "finish":
                    if identity in self._finished:
                        self.violations.append(("I20", "work ended more than once"))
                    self._finished[identity] = event.monotonic_ns
                    self.coverage["finished"] += 1
                    self.by_kind[(identity.work_kind.value, "finish")] += 1
                elif event.operation == "apply":
                    if (identity in self._finished or event.window.expired(event.monotonic_ns)
                            or self._windows.get(identity) != event.window):
                        self.violations.append(("I19", "application event followed retirement or expiry"))
                    self._applications.add((identity, event.monotonic_ns))
            for identity, _resource in owner.resources:
                if identity is None or identity != owner.active_identity or identity in self._finished:
                    self.violations.append(("I20", "retired or foreign work still owns a resource"))
            if owner.active_identity is not None:
                if owner.active_identity in self._finished:
                    self.violations.append(("I20", "retired identity is active"))
                if self._windows.get(owner.active_identity) != owner.active_window:
                    self.violations.append(("I18", "active window differs from creation record"))
            for record in owner.admissions:
                key = (record.identity, "admission", record.accepted_monotonic_ns, record.disposition)
                if key in self._seen:
                    continue
                self._seen.add(key)
                self.last_events.append(record)
                if record.disposition is not AsyncAdmissionDisposition.APPLIED:
                    self.coverage["not_applied"] += 1
                    continue
                self.coverage["applied"] += 1
                self.by_kind[(record.identity.work_kind.value, "apply")] += 1
                window = self._windows.get(record.identity)
                ended = self._finished.get(record.identity)
                if (window is None or not record.identity_matched or record.facts_valid is not True
                        or (record.identity, record.accepted_monotonic_ns) not in self._applications
                        or record.deadline_monotonic_ns != window.deadline_monotonic_ns
                        or record.accepted_monotonic_ns >= window.deadline_monotonic_ns
                        or (ended is not None and record.accepted_monotonic_ns > ended)):
                    self.violations.append(("I19", "result applied without current identity, facts or time"))
                if record.identity.work_kind is AsyncWorkKind.INFORMATION:
                    queries = tuple(q for q in owner.fact_queries if q.identity == record.identity and q.acquired)
                    if not queries or any(q.cell_fact.knowledge is CellKnowledge.UNKNOWN for q in queries):
                        self.violations.append(("I21", "information applied without a determined fact query"))
                    else:
                        self.coverage["information_applied"] += 1

    def finalize(self, requirement, observed_activity):
        if type(requirement) is not AsyncCoverageRequirement or observed_activity is None:
            return VerificationAssessment(VerificationStatus.UNASSESSED, {},
                (VerificationGap("I18-I22", "coverage or independent activity not assessed"),))
        gaps = [VerificationGap(rule, detail) for rule, detail in self.violations]
        actual = {record.identity for record in observed_activity}
        if requirement.no_async_work:
            if actual or self._windows:
                gaps.append(VerificationGap("I18", "declared no work but real activity occurred"))
        else:
            for kind in requirement.required_kinds:
                if not self.by_kind[(kind.value, "begin")]:
                    gaps.append(VerificationGap("I18", f"missing {kind.value} creation record"))
            for kind in requirement.applied_kinds:
                if not self.by_kind[(kind.value, "apply")]:
                    gaps.append(VerificationGap("I19", f"missing {kind.value} normal application"))
        for identity in actual:
            if identity is None or identity not in self._windows:
                gaps.append(VerificationGap("I18", "independent activity has no creation record"))
            if identity is None or identity not in self._finished:
                gaps.append(VerificationGap("I20", "independent activity has no finish record"))
        for identity in self._windows:
            if identity not in self._finished:
                gaps.append(VerificationGap("I20", "created work did not finish"))
        coverage = {kind.value: {operation: self.by_kind[(kind.value, operation)]
                                for operation in ("begin", "apply", "finish")}
                    for kind in AsyncWorkKind}
        return VerificationAssessment(
            VerificationStatus.INCOMPLETE if gaps else VerificationStatus.VERIFIED,
            coverage, tuple(gaps))
