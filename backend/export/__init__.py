"""JFW-4: Export-Vertragskern (I/O-frei).

Verlustfreies JSON ``jfw4_export_v1`` als Set-Autoritaet, optionale SRT/VTT-
Präsentationsableitungen, atomarer Set-Commit. Die Module sind rein funktional
und ohne Dateisystem/DB testbar; I/O liegt ausschliesslich in
``services/export_contract.py``, ``routes/export.py`` und ``set_writer.py``
(injizierbare FS-Operationen).
"""
