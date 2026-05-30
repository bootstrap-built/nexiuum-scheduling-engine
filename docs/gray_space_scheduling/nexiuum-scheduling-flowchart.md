# Nexiuum / Gray Space Scheduling — Process Flowchart

A walk-through of how a customer order moves through the system from intake to shipment. Designed to share with Jason, Victoria, Adrian, Makayla, and the team.

---

## 1. End-to-end flow

```mermaid
flowchart TD
    Customer([Customer places order]) --> AM
    AM[Account Manager fills out<br/>Spec Sheet form] --> PS

    subgraph Nexiuum[Nexiuum Monday — Production Schedule]
        PS[One item created per flavor<br/>with full spec sheet payload]
        Recipe[Recipe assigned<br/>e.g., tablet → blister → clamshell]
        PS --> Recipe
    end

    Recipe --> Engine

    subgraph Engine[Scheduling Engine]
        Stages[Splits recipe into stages]
        Routing[Picks the right machine<br/>for each stage]
        Placement[Computes start/end times<br/>around existing schedule]
        Stages --> Routing --> Placement
    end

    Placement --> GSboard[(Gray Space Schedule<br/>— pressing slots)]
    Placement --> NXboard[(Nexiuum Schedule<br/>— packaging slots)]

    GSboard --> Zane[Zane runs the press job<br/>Status → Pressing → Complete]
    Zane --> Baton{Baton pass<br/>press done → packaging unlocked}

    NXboard --> Baton
    Baton --> Makayla[Makayla / packaging team<br/>runs blister + clamshell job<br/>Status → Running → Complete]
    Makayla --> Ship([Ship to customer])

    classDef customer fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#000
    classDef people fill:#dbeafe,stroke:#2563eb,color:#000
    classDef board fill:#e0e7ff,stroke:#4338ca,color:#000
    classDef engine fill:#dcfce7,stroke:#16a34a,color:#000
    classDef done fill:#fce7f3,stroke:#be185d,color:#000

    class Customer,Ship customer
    class AM,Zane,Makayla people
    class PS,Recipe,GSboard,NXboard board
    class Stages,Routing,Placement engine
    class Baton done
```

---

## 2. What the Spec Sheet form captures (zoom-in)

```mermaid
flowchart LR
    AM[Account Manager] --> Form

    subgraph Form[Spec Sheet Form]
        Hdr[Header fields<br/>Client, PO#, Mfg Route]
        Prod[Product type<br/>Tablet / Capsule / Pouch / etc.]
        Flav[Per-flavor lines<br/>actives, qty, packaging mode]
        Hdr --> Prod --> Flav
    end

    Form --> Payload[Single JSON payload written<br/>to Production Schedule item]
    Payload --> Trigger[Monday automation fires<br/>when Recipe Key populates]
    Trigger --> Engine([Scheduling Engine /commit])

    classDef person fill:#dbeafe,stroke:#2563eb,color:#000
    classDef form fill:#fef3c7,stroke:#d97706,color:#000
    classDef data fill:#e0e7ff,stroke:#4338ca,color:#000

    class AM person
    class Hdr,Prod,Flav form
    class Payload,Trigger data
```

**Manufacturing Route field** drives what the engine schedules:

| Route | What happens |
|---|---|
| Manufacturing | Press only (Gray Space) |
| Manufacturing + Packaging | Press → packaging DAG (Gray Space → Nexiuum) |
| Packaging | Packaging only (Nexiuum) — tablets already on hand |
| Ship Bulk | Press only, then ship bulk product |
| Keep for Packaging | Press now, package later |
| Hot Shot / Samples | TBD with Makayla |

---

## 3. How the engine picks a machine (zoom-in)

```mermaid
flowchart TD
    Stage[Stage to place<br/>e.g., 'blister' for 50,000 tablets] --> Class

    Class{What machine class<br/>does this stage need?}
    Class -->|Pressing| GS[Look at Gray Space<br/>Capacity Engine]
    Class -->|Blister / Clamshell / Sachet / Bottle| NX[Look at Nexiuum<br/>Capacity Engine]

    GS --> Filter
    NX --> Filter

    Filter[Filter to machines that are:<br/>• Online<br/>• Have capacity > 0<br/>• Pass hard rules<br/>  dual-sided / force-route / job-size cap]

    Filter --> Pick[For each eligible machine,<br/>compute earliest start considering:<br/>• Predecessor stage's end time<br/>• Existing queue on this machine<br/>• Daily working hours window<br/>• Changeover buffer]

    Pick --> Best[Pick the machine with<br/>the soonest available start]

    Best --> Slot[Write slot to that instance's<br/>Schedule board]

    classDef question fill:#fef3c7,stroke:#d97706,color:#000
    classDef logic fill:#dcfce7,stroke:#16a34a,color:#000
    classDef output fill:#e0e7ff,stroke:#4338ca,color:#000

    class Class question
    class GS,NX,Filter,Pick logic
    class Best,Slot output
```

**Hard routing rules** are non-negotiable physical constraints on the machines:

| Rule | Example |
|---|---|
| Dual-sided only | Penn & Teller — only runs dual-sided tablets |
| Force-route by condition | Lancelot — anything with active > 80mg |
| Max job size | Copperfield — 10,000 tabs max (R&D line) |

**Soft routing** for the rest: round-robin by least-recently-used machine, so wear levels out.

---

## 4. The baton pass — press → packaging

```mermaid
sequenceDiagram
    participant Zane as Zane (Gray Space)
    participant GSboard as Gray Space Schedule
    participant Engine
    participant NXboard as Nexiuum Schedule
    participant Makayla as Makayla (Packaging)

    Note over Engine: Press slot + packaging slot<br/>both written at intake.<br/>Packaging slot waits.

    Zane->>GSboard: Set status → Pressing
    GSboard->>Engine: Webhook fires
    Engine->>GSboard: Writes actual_start
    Engine-->>NXboard: Marks packaging slot<br/>"Predecessor running"

    Note over Zane: Press job runs

    Zane->>GSboard: Set status → Complete
    GSboard->>Engine: Webhook fires
    Engine->>GSboard: Writes actual_end
    Engine->>NXboard: Releases packaging slot<br/>(eligible to start now)

    NXboard->>Makayla: Slot appears in queue<br/>(or top of queue)

    Note over Makayla: Packaging job runs

    Makayla->>NXboard: Set status → Running → Complete
    NXboard->>Engine: Webhook fires
    Engine->>NXboard: Writes actual_start / actual_end
```

The engine reads the Process Recipe to know which stages depend on which. When the press stage finishes, the engine looks up every later stage in that recipe and unlocks them. No manual hand-off needed.

---

## 5. Two Schedule boards, one unified view

```mermaid
flowchart LR
    subgraph GSworkspace[Gray Space Monday]
        GSboard[(Gray Space Schedule<br/>— press slots)]
        GSusers[Zane + Gray Space ops<br/>see press queue natively]
        GSboard --> GSusers
    end

    subgraph NXworkspace[Nexiuum Monday]
        NXboard[(Nexiuum Schedule<br/>— packaging slots)]
        NXusers[Makayla + packaging team<br/>see packaging queue natively]
        NXboard --> NXusers
    end

    subgraph Embedded[Marey chart embedded view]
        Chart[Live timeline showing<br/>ALL stages of ALL jobs<br/>across both instances]
    end

    GSboard --> Chart
    NXboard --> Chart

    Chart --> AMview[Account managers + Jason<br/>see end-to-end timeline<br/>in Nexiuum workspace]

    classDef ws fill:#f3f4f6,stroke:#6b7280,color:#000
    classDef board fill:#e0e7ff,stroke:#4338ca,color:#000
    classDef view fill:#dcfce7,stroke:#16a34a,color:#000
    classDef people fill:#dbeafe,stroke:#2563eb,color:#000

    class GSworkspace,NXworkspace,Embedded ws
    class GSboard,NXboard board
    class Chart view
    class GSusers,NXusers,AMview people
```

**Why two boards instead of one:**
- Each operator team sees their own schedule natively in their workspace
- Cross-account linking in Monday is fragile (admin permissions can revoke)
- The embedded Marey view stitches both together so AMs and leadership get the unified picture

---

## 6. Who does what — role responsibilities

```mermaid
flowchart TB
    subgraph AMrow[Account Managers — Adrian, Bella, others]
        AMcols[• Fill out Spec Sheet form<br/>• Use CTP simulator to quote ship dates<br/>• Watch Marey chart for status]
    end

    subgraph OPSrow[Production COO — Victoria]
        OPScols[• Maintain Capacity Engine boards<br/>  machine status, hours, capacity<br/>• Adjust capacity when machines go down<br/>• Approve recipe changes]
    end

    subgraph GSrow[Gray Space ops — Zane]
        GSops[• Run press jobs<br/>• Flip Blend Status → Pressing → Complete<br/>• Fill blend records as usual]
    end

    subgraph NXrow[Packaging team — Makayla]
        NXops[• Run packaging jobs<br/>• Flip Schedule status → Running → Complete<br/>• Maintain Nexiuum Capacity Engine entries]
    end

    subgraph ENrow[Scheduling Engine — automated]
        ENbox[• Reads recipes + capacities<br/>• Places slots on both Schedule boards<br/>• Writes actual_start / actual_end from status flips<br/>• Reflows around capacity changes<br/>• Serves the unified Marey view]
    end

    classDef am fill:#dbeafe,stroke:#2563eb,color:#000
    classDef ops fill:#fef3c7,stroke:#d97706,color:#000
    classDef gs fill:#e0e7ff,stroke:#4338ca,color:#000
    classDef nx fill:#dcfce7,stroke:#16a34a,color:#000
    classDef en fill:#fce7f3,stroke:#be185d,color:#000

    class AMrow,AMcols am
    class OPSrow,OPScols ops
    class GSrow,GSops gs
    class NXrow,NXops nx
    class ENrow,ENbox en
```

---

## 7. CTP — quote a realistic ship date before committing

When an AM is talking to a customer and needs to know "when can you get me 500k tablets?":

```mermaid
flowchart LR
    AMq[AM enters<br/>product mix + qty] --> Sim[/simulate endpoint/]
    Sim --> Engine2[Engine runs the same<br/>routing + placement logic<br/>against the current schedule]
    Engine2 --> Date[Returns:<br/>• Projected ship date<br/>• +20% safety pad<br/>• Which machine is the bottleneck]
    Date --> AMq2[AM quotes the padded date]
    AMq2 --> Customer([Customer hears realistic date])

    classDef person fill:#dbeafe,stroke:#2563eb,color:#000
    classDef engine fill:#dcfce7,stroke:#16a34a,color:#000
    classDef output fill:#e0e7ff,stroke:#4338ca,color:#000

    class AMq,AMq2 person
    class Sim,Engine2 engine
    class Date output
    class Customer person
```

No writes happen — `/simulate` is read-only. It just tells the AM what the schedule would say if this order landed now.

---

## Where we are today (May 2026)

| Piece | Status |
|---|---|
| Gray Space pressing engine | Live on bb-infra-01, production-verified |
| Gray Space Capacity Engine board | Live, 7 machines |
| Nexiuum Capacity Engine board | Built, 22 packaging machines, ~13 awaiting Makayla's capacity numbers |
| Cross-instance scheduling | Built and tested locally, not yet deployed |
| Spec Sheet form | Adrian is building it |
| Baton pass (press → packaging) | Designed, next on the build list |
| Marey chart (Gray Space only) | Live |
| Marey chart (multi-instance) | Pending |
| Live production verify | After all of the above land |

---

*Generated 2026-05-25. Update when the build state changes.*
