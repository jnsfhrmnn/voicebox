"""JFW-11: Dual-Source-Meetingaufnahme — Vertragskern (I/O-frei).

Module: ``time_model`` (QPC-Zeitbasis, Offset+Drift), ``tracks`` (zwei
autoritative Rohspuren), ``manifest`` (atomarer Capture-Manifest), ``run_state``
(Zustandsmaschine), ``recovery`` (Verlustfenster), ``dedupe`` (konservative
Eigenstimmen-Deduplizierung), ``naming`` (reversible Namenszuordnung),
``contract``/``provenance`` (Ergebnis-/Hash-Vertrag). Schreibpfad ausschließlich
``services/meeting_contract.py``.
"""
