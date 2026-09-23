"""JFW-6: Aufnahme-Vertragskern (I/O-frei).

Nur echte Gaps gegen die gemessene Voicebox-Baseline: Aufnahmezustand
(``contract``), Sound-Cues (``cues``), Recovery-Grenzen (``recovery``) und
Run-Identität (``run_identity``) — dazu Manifest (``manifest``) und
Provenienz (``provenance``) fuer den idempotenten JFW-7-Handoff. Die
browserbasierte ``MediaRecorder``-Aufnahme bleibt die einzige
Aufnahmeautorität; dieses Paket haertet ausschliesslich darunter.
"""
