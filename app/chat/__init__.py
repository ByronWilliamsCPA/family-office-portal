# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Chat: answer questions from the documents and the balance table (ADR-009).

Modules: ``settings`` (every chat setting name), ``balances`` (the balance
table), ``prompt`` (system prompt assembly), ``images`` (one image, at most
1024 px), ``client`` (the model call), ``render`` (plain-text answers and
citation links), and ``service`` (one question end to end).
"""
