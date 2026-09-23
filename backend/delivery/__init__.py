"""JFW-8: Vertragskern der sicheren appuebergreifenden Zieluebergabe (I/O-frei).

Module: ``payload`` (JFW-7-Handoff-Ansprache + Textmodus-Riegel + Idempotenz),
``target`` (Ziel-Snapshot und Trust Boundary), ``contract`` (Delivery State
Contract inkl. Exactly-once-Budget), ``verification`` (Scroll-Lock-Vertrag und
Verifikations-Doppelauslosung), ``clipboard`` (verifizierter Paste-Pfad),
``outcome`` (Zielergebnis-Auswertung), ``recovery`` (Recovery-Queue mit klaren
Grenzen), ``trace`` (inhaltsfreie Fehlerpfad-Spuren), ``provenance``
(Identity-/Payload-Hash-Bindung). Alle Module sind rein und ohne I/O
testbar; externe Windows-Mechanismen laufen ausschliesslich hinter
injizierbaren Seams (fail-closed ``provider_runtime_missing``).
"""
