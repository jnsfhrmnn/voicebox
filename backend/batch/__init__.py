"""JFW-5: Batch-Verarbeitung — I/O-freier Vertragskern.

Stapelverarbeitung als Abarbeitung in Serie mit unveraendertlichem
Batch-Snapshot, Ressourcengrenzen, Retry und Resume OHNE Verlust bereits
gesicherter Ergebnisse. Optionale Profilstufen JFW-4-Export und JFW-13-Protokoll
werden als fertige Vertraege gebunden konsumiert (Reuse statt Neu-Erfinden).
Bewusst KEINE Ordnerueberwachung (Out of Scope der Spec).
"""
