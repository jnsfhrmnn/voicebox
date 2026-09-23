"""JFW-7: Vertragskern der Diktatpipeline (I/O-frei).

Genau ein unverändertes STT-Rohtranskript — das JFW-9-Verbot ist Vertragsbestandteil:
KEIN Text-LLM, KEIN Refinement am Rohtranskript, NIE. Diese Paket besitzt keinen
LLM-/Refine-/Translate-Pfad; verbotene Ergebnis-Kinds sind fail-closed blockiert.
"""
