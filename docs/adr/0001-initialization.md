# ADR 0001: Independent package and reference models

Status: adopted.

Keep Chronicle in an independent Python package with no shared writes or synchronous dependency on another application. Pure contracts describe time, identity, units and exact arithmetic. Independent full-scan and rational reference implementations check observable results against hand-authored cases.

Selection, formula and rounding helpers are not imported into the independent oracles. Database privileges and concurrency are checked against PostgreSQL rather than inferred from pure tests. Numerical agreement alone does not establish source authenticity.
