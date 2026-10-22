"""Friction analysis pipeline: export -> anonymized shapes -> replay -> buckets -> report.

Every stage reads and writes plain JSON in one work directory, so any stage can be
re-run on its own and every intermediate file can be inspected. Stdlib only.
"""
