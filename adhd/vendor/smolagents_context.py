# Adapted from huggingface/smolagents src/smolagents/utils.py.
# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
# Licensed under the Apache License, Version 2.0. See third_party/Apache-2.0.txt.
# Source blob: 69ade34311ce18217a008fb3a41cb0da11f0826d (lines 254-265).
# Change: reserve space for the marker; enforce a real character ceiling,
# including very small budgets. Character count is NOT a token count.
def truncate_content(content: str, max_length: int = 3500) -> str:
    if max_length < 0:
        raise ValueError('max_length must be nonnegative')
    if len(content) <= max_length:
        return content
    marker = '\n...[truncated; read the referenced file]...\n'
    if max_length <= len(marker):
        return content[:max_length]
    budget = max_length - len(marker)
    left = (budget + 1) // 2
    right = budget // 2
    return content[:left] + marker + (content[-right:] if right else '')
